"""Тестовый скрипт: отправка на TEST_EMAIL с данными первого врача из таблицы."""
import os
import re
import json
from datetime import datetime
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

# Regex для разбора отметок (тот же, что в receiver.py)
CHECKBOX_RE = re.compile(
    r"^[\[(]\s*([^\s\]\)]?)\s*[\])]\s*(\d{1,3})([АБВABabабв])?(?![0-9])"
)
LATIN_TO_CYR = {"A": "А", "B": "В"}


def parse_reply(body, mapping):
    """Разбор отметок-квадратиков в ответе врача."""
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


def simulate_doctor_reply(original_body, bot_id, mark_keys):
    """Симулирует ответ врача с отметками указанных заданий."""
    lines = []
    lines.append(f"> {original_body.splitlines()[0]}")
    lines.append(">")
    lines.append(f"> Re: Задания для пациентов на {now_moscow().strftime('%d.%m.%Y')} [BOTON-ID:{bot_id}]")
    lines.append(">")
    lines.append("")
    
    for line in original_body.splitlines():
        if line.startswith("      [ ]"):
            m = re.search(r"\[ \]\s*(\d+[АБВ]?)", line)
            if m:
                key = m.group(1)
                if key in mark_keys:
                    line = line.replace("[ ]", "[x]")
            lines.append(line)
        elif line.startswith("   [ ]"):
            m = re.search(r"\[ \]\s*(\d+)", line)
            if m:
                key = m.group(1)
                if key in mark_keys:
                    line = line.replace("[ ]", "[x]")
            lines.append(line)
        else:
            lines.append(line)
    
    return "\n".join(lines)


def test_send_to_first_doctor():
    """Отправка тестового письма на TEST_EMAIL с данными первого врача."""
    print("\n" + "=" * 70)
    print(" ТЕСТОВАЯ ОТПРАВКА НА TEST_EMAIL (данные первого врача)")
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
    print(f"   👨‍⚕️ Загружено врачей: {len(doctors)}")
    
    # 2. Загружаем строки
    print(f"\n2️⃣ Загрузка строк за {target_date.strftime('%d.%m.%Y')}...")
    rows, stats = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)
    
    if not rows:
        print("\n   ❌ НЕТ СТРОК ДЛЯ ТЕСТИРОВАНИЯ!")
        print("   💡 Добавьте тестовую строку с сегодняшней датой и пустым столбцом N.")
        return
    
    # 3. Берём ПЕРВОГО врача из списка строк
    print("\n3️⃣ Выбор ПЕРВОГО врача из таблицы заданий...")
    grouped = group_by_doctor(rows)
    first_doctor_name = list(grouped.keys())[0]
    doctor_rows = grouped[first_doctor_name]
    
    doctor = find_doctor_robust(first_doctor_name, doctors)
    if not doctor:
        print(f"   ❌ Врач '{first_doctor_name}' не найден в базе")
        return
    
    print(f"   ✅ Тестовые данные врача: {doctor['name']}")
    print(f"   📧 Реальный email врача: {doctor.get('email', 'НЕТ')}")
    print(f"   📧 Письмо уйдёт на TEST_EMAIL: {TEST_EMAIL}")
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
    
    # 5. Отправляем письмо на TEST_EMAIL
    print(f"\n5️⃣ Отправка письма на {TEST_EMAIL}...")
    try:
        send_email(TEST_EMAIL, subject, body)
        print(f"   ✅ Письмо успешно отправлено на {TEST_EMAIL}!")
    except Exception as exc:
        print(f"   ❌ Ошибка отправки: {exc}")
        return
    
    # 6. Симулируем ответ врача
    print("\n6️⃣ Симуляция ответа врача...")
    mark_keys = list(mapping.keys())[:3]  # Отмечаем первые 3 задания
    print(f"   📝 Отмечаем задания: {mark_keys}")
    
    reply_body = simulate_doctor_reply(body, bot_id, mark_keys)
    
    # 7. Разбираем ответ
    print("\n7️⃣ Разбор ответа...")
    answers = parse_reply(reply_body, mapping)
    
    if not answers:
        print("   ❌ Отметки не найдены в сгенерированном ответе!")
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
            print(f"   ⚠️ Нет ячеек для записи")
    
    # 9. Итог
    print("\n" + "=" * 70)
    print(" ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
    print("=" * 70)
    print(f"✅ Письмо отправлено на {TEST_EMAIL}")
    print(f"✅ Ответ симулирован с {len(mark_keys)} отметками")
    print(f"✅ Разбор выполнен: {len(answers)} статусов")
    print(f"\n📧 Проверьте почту {TEST_EMAIL} — там должно быть тестовое письмо.")
    print(f"📋 Если выбрали 'y' — результаты записаны в таблицу.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    test_send_to_first_doctor()
