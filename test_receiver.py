"""Тестовый скрипт: отправка на IMAP-ящик → ожидание ответа → разбор → запись."""
import os
import re
import json
import time
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Импорты из проекта
from config import *
from utils import normalize_text, normalize_email, now_moscow
from tasks import load_task_rows, print_stats, build_patients, build_response_task_map, group_by_doctor
from doctors import find_doctor_robust, load_doctors
from log_store import append_log_entries
from google_sheets import batch_update_cells
from mailer import build_email_body, send_email
import gspread
from google.oauth2.service_account import Credentials

# Regex для разбора отметок
CHECKBOX_RE = re.compile(
    r"^[\[(]\s*([^\s\]\)]?)\s*[\])]\s*(\d{1,3})([АБВABabабв])?(?![0-9])"
)
LATIN_TO_CYR = {"A": "А", "B": "В"}


def decode_mime_header(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def extract_message_body(msg):
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(cs, errors="replace"))
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    cs = msg.get_content_charset() or "utf-8"
    return payload.decode(cs, errors="replace")


def extract_bot_id(subject, body):
    text = f"{subject}\n{body}"
    m = re.search(r"BOTON-ID\s*[:\-]?\s*(BOTON-[A-Za-z0-9_.\-]+)", text, flags=re.IGNORECASE)
    return m.group(1) if m else ""


def parse_reply(body, mapping):
    """Разбор отметок-квадратиков в ответе."""
    done = set()
    seen = False
    
    for raw_line in body.splitlines():
        line = re.sub(r"^[\s>]+", "", raw_line)
        m = CHECKBOX_RE.match(line)
        if not m:
            continue
        
        digits = str(int(m.group(2)))
        letter = (m.group(3) or "").upper()
        letter = LATIN_TO_CYR.get(letter, letter)
        
        key = f"{digits}{letter}"
        if key in mapping:
            keys = [key]
        elif not letter:
            keys = [k for k in mapping if re.fullmatch(rf"{digits}[АБВ]", k)]
        else:
            keys = []
        
        if not keys:
            continue
        
        mark = m.group(1)
        if mark:
            seen = True
            done.update(keys)

    if not seen:
        return None

    result = {}
    for key in mapping:
        if key in done:
            result[key] = STATUS_DONE
        else:
            result[key] = STATUS_NOT_DONE
    return result


def wait_for_reply(bot_id, timeout_minutes=10):
    """Ждёт ответное письмо с указанным BOTON-ID в IMAP-ящике."""
    print(f"\n⏳ Ожидание ответа на BOTON-ID: {bot_id}")
    print(f"   Таймаут: {timeout_minutes} минут")
    
    host = os.environ.get("IMAP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER", "")
    password = os.environ.get("IMAP_PASSWORD", "")
    
    if not all([host, user, password]):
        print("   ❌ IMAP не настроен! Проверьте секреты IMAP_HOST, IMAP_USER, IMAP_PASSWORD")
        return None
    
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    
    mail = None
    try:
        print(f"   Подключение к {host}:{port}...")
        mail = imaplib.IMAP4_SSL(host, port, timeout=20)
        mail.login(user, password)
        mail.select("INBOX", readonly=True)
        print(f"   ✅ Подключено к {user}")
        
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                print(f"\n    Таймаут! Ответ не получен за {timeout_minutes} минут.")
                return None
            
            # Ищем письма с BOTON-ID в теме
            search_query = f'(SUBJECT "{bot_id}")'
            status, data = mail.search(None, search_query)
            
            if status == "OK" and data[0]:
                nums = data[0].split()
                if nums:
                    # Берём последнее письмо (самое свежее)
                    num = nums[-1]
                    st, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                    if st == "OK" and msg_data[0]:
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)
                        subject = decode_mime_header(msg.get("Subject", ""))
                        body = extract_message_body(msg)
                        
                        print(f"\n   ✅ Получено ответное письмо!")
                        print(f"   Тема: {subject}")
                        return body
            
            remaining = int(timeout_seconds - elapsed)
            print(f"   ⏳ Прошло {int(elapsed)} сек. Осталось {remaining} сек...", end="\r")
            time.sleep(5)  # Проверяем каждые 5 секунд
    
    except Exception as exc:
        print(f"\n   ❌ Ошибка IMAP: {exc}")
        return None
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass


def test_full_cycle_with_reply():
    """Полный тест: отправка → ожидание ответа → разбор → запись."""
    print("\n" + "=" * 70)
    print(" ТЕСТОВЫЙ ЦИКЛ: ОТПРАВКА → ОТВЕТ → РАЗБОР → ЗАПИСЬ")
    print("=" * 70)
    
    # 1. Загружаем данные
    print("\n1️⃣ Загрузка данных...")
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    
    tasks_spreadsheet = gc.open_by_key(TASKS_SPREADSHEET_ID)
    target_date = now_moscow().date()
    sheet_name = target_date.strftime(TASKS_SHEET_DATE_FORMAT)
    
    try:
        tasks_sheet = tasks_spreadsheet.worksheet(sheet_name)
        print(f"   ✅ Открыта вкладка: {sheet_name}")
    except gspread.WorksheetNotFound:
        print(f"   ⚠️ Вкладка '{sheet_name}' не найдена. Используем fallback.")
        try:
            tasks_sheet = tasks_spreadsheet.worksheet(TASKS_SHEET_NAME_FALLBACK)
        except gspread.WorksheetNotFound:
            print("   ❌ Fallback-вкладка тоже не найдена!")
            return
    
    doctors_sheet = gc.open_by_key(DOCTORS_SPREADSHEET_ID).worksheet(DOCTORS_SHEET_NAME)
    doctors = load_doctors(doctors_sheet)
    print(f"   👨⚕️ Загружено врачей: {len(doctors)}")
    
    # 2. Загружаем строки
    print(f"\n2️⃣ Загрузка строк за {target_date.strftime('%d.%m.%Y')}...")
    rows, stats = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)
    
    if not rows:
        print("\n   ❌ НЕТ СТРОК ДЛЯ ТЕСТИРОВАНИЯ!")
        print("   💡 Добавьте тестовую строку с сегодняшней датой и пустым столбцом N.")
        return
    
    # 3. Берём первого врача
    print("\n3️⃣ Выбор первого врача...")
    grouped = group_by_doctor(rows)
    first_doctor_name = list(grouped.keys())[0]
    doctor_rows = grouped[first_doctor_name]
    
    doctor = find_doctor_robust(first_doctor_name, doctors)
    if not doctor:
        print(f"   ❌ Врач '{first_doctor_name}' не найден в базе")
        return
    
    print(f"   ✅ Тестовый врач: {doctor['name']}")
    print(f"   📋 Строк заданий: {len(doctor_rows)}")
    
    # 4. Генерируем письмо
    print("\n4️⃣ Генерация тестового письма...")
    patients = build_patients(doctor_rows)
    mapping = build_response_task_map(patients)
    sig = doctor["name"][:3].upper()
    bot_id = f"BOTON-TEST-{target_date.strftime('%Y%m%d')}-{sig}-{datetime.now().strftime('%H%M%S')}"
    
    body = build_email_body(doctor, patients, bot_id)
    subject = f"[ТЕСТ] Задания для пациентов на {target_date.strftime('%d.%m.%Y')} [BOTON-ID:{bot_id}]"
    
    print(f"   ✅ Письмо сгенерировано")
    print(f"   📝 Тема: {subject}")
    print(f"   📦 Заданий: {len(mapping)}")
    
    # 5. Отправляем письмо на IMAP_USER (тот ящик, который проверяется)
    imap_user = os.environ.get("IMAP_USER", "")
    if not imap_user:
        print("   ❌ IMAP_USER не настроен!")
        return
    
    print(f"\n5️⃣ Отправка письма на {imap_user}...")
    print(f"   ️ Отвечайте на это письмо через IMAP-ящик ({imap_user})!")
    
    try:
        send_email(imap_user, subject, body)
        print(f"   ✅ Письмо отправлено на {imap_user}")
    except Exception as exc:
        print(f"   ❌ Ошибка отправки: {exc}")
        return
    
    # 6. Ждём ответ
    print("\n6️⃣ Ожидание вашего ответа...")
    print(f"   📧 Откройте почту {imap_user}")
    print(f"    Найдите письмо с темой: [ТЕСТ] Задания для пациентов...")
    print(f"   ✏️ Нажмите 'Ответить' и поставьте [x] в нужных заданиях")
    print(f"   📤 Отправьте ответ")
    
    reply_body = wait_for_reply(bot_id, timeout_minutes=10)
    
    if not reply_body:
        print("\n   ❌ Ответ не получен!")
        return
    
    # 7. Разбираем ответ
    print("\n7️⃣ Разбор ответа...")
    answers = parse_reply(reply_body, mapping)
    
    if not answers:
        print("   ❌ Отметки не найдены в ответе!")
        print("   💡 Убедитесь, что вы поставили [x] в квадратиках")
        return
    
    print(f"   ✅ Найдено отметок: {len(answers)}")
    print(f"\n   Результат разбора:")
    for key, status in answers.items():
        mark = "✅" if status == STATUS_DONE else "❌"
        print(f"      {mark} {key}: {status}")
    
    # 8. Записываем в таблицу
    print("\n8️⃣ Запись в таблицу...")
    write_to_table = input("   Записать результаты в таблицу? (y/n, по умолчанию y): ").strip().lower()
    if write_to_table == 'n':
        print("   ⏭️ Пропущено")
    else:
        updates = []
        for key, status_value in answers.items():
            for item in mapping.get(key, []):
                row = item["row_number"]
                status_col = item["status_col"]
                updates.append({
                    "row": row,
                    "col": status_col + 1,
                    "value": status_value,
                })
        
        if updates:
            print(f"   📝 Записываем {len(updates)} ячеек...")
            batch_update_cells(tasks_sheet, updates, "тестовая запись")
            print(f"   ✅ Записано в таблицу!")
        else:
            print(f"   ️ Нет ячеек для записи")
    
    # 9. Итог
    print("\n" + "=" * 70)
    print(" ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
    print("=" * 70)
    print(f"✅ Письмо отправлено на {imap_user}")
    print(f"✅ Ответ получен и разобран: {len(answers)} статусов")
    if write_to_table != 'n':
        print(f"✅ Результаты записаны в таблицу")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    test_full_cycle_with_reply()
