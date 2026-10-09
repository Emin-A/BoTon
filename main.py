# ============================================================
# BoTon v3.2
# - Чтение данных ВСЕГДА из Лист1 (рабочий лист на сегодня/завтра)
# - Фильтр строк по столбцу A = сегодня МСК
# - Журнал BoTon_Log + BOTON-ID (защита от дублей)
# - IMAP-приём ответов врачей → H/J/L
# - Plain-text письма и отчёт (не HTML)
# - Retry Google Sheets (429/5xx)
# - Режимы: FORCE_RESEND, TEST_TO_RESERVE, TEST_RECEIVE_ONLY
# - ДИАГНОСТИКА: TEST_DOCTOR_DIAG — 1 письмо любого врача → TEST_EMAIL
# - Окно рассылки 08:00–12:00 МСК
#
# VERSION: 3.2
# ============================================================

import os
import re
import json
import ssl
import smtplib
import hashlib
import imaplib
import email
from email.header import decode_header
from email.message import EmailMessage
from collections import defaultdict
from datetime import datetime, timedelta, time
from time import sleep
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIG
# ============================================================

VERSION = "3.2"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ---------- РЕЖИМЫ ----------
FORCE_RESEND = False       # True = игнорировать журнал и время, отправить всё заново
TEST_TO_RESERVE = False    # True = все письма врачам → на REPORT_EMAIL (для проверки)
TEST_RECEIVE_ONLY = False  # True = только приём ответов, без рассылки

# ДИАГНОСТИКА: одно письмо любого врача → TEST_EMAIL. Прерывает прогон.
TEST_DOCTOR_DIAG = False
TEST_EMAIL = "agaverdiev@mail.ru"

REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"   # почта заведующего и резерв

# ---------- GOOGLE SHEETS ----------
TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"                    # ВСЕГДА читаем отсюда
DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"
LOG_SHEET_NAME = "BoTon_Log"

# ---------- СТОЛБЦЫ (0-индексные) ----------
COL_DATE = 0
COL_BRANCH = 1
COL_DOCTOR = 2
COL_PATIENT_ID = 3
COL_PATIENT_NAME = 4
COL_SOURCE = 5
COL_TASK1 = 6
COL_STATUS1 = 7
COL_TASK2 = 8
COL_STATUS2 = 9
COL_TASK3 = 10
COL_STATUS3 = 11
COL_COMPLETION = 12

# ---------- ФИЛИАЛ ----------
BRANCH_KEYWORDS = [
    "ф2", "филиал 2", "филиал № 2", "гп 107 ф 2",
    "гп 48", "107 ф 2", "гп 107 ф2",
]

TASK_LETTERS = ["А", "Б", "В"]
TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}

# ---------- ВРЕМЯ РАССЫЛКИ (МСК) ----------
ASSIGNMENT_HOUR_START = 8
ASSIGNMENT_HOUR_END = 12

# ---------- GOOGLE SHEETS RETRY ----------
SHEETS_RETRY_ATTEMPTS = 5
SHEETS_RETRY_BASE_SECONDS = 1.5

# ---------- IMAP ----------
IMAP_TIMEOUT = 20
IMAP_LOOKBACK_DAYS = 3
IMAP_MAX_PER_DOCTOR = 20
IMAP_MAX_TOTAL = 100


# ============================================================
# ВРЕМЯ
# ============================================================

def now_moscow():
    return datetime.now(MOSCOW_TZ)


def today_str():
    return now_moscow().strftime("%d.%m.%Y")


def is_assignment_time():
    t = now_moscow().time()
    return time(ASSIGNMENT_HOUR_START, 0) <= t <= time(ASSIGNMENT_HOUR_END, 0)


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================

def normalize_text(value):
    if value is None:
        return ""
    value = str(value).replace("Ё", "Е").replace("ё", "е").replace("\xa0", " ")
    value = re.sub(r"[^\w\sА-Яа-я]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def normalize_email(value):
    return str(value or "").strip().lower()


def get_signature(name):
    """ФИО → 'фамилия инициалы' (устойчиво к перестановкам и точкам)."""
    if not name:
        return ""
    clean = str(name).lower().replace("ё", "е")
    clean = re.sub(r"[^a-zа-яё\s.]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    parts = [p for p in clean.split() if p]
    if not parts:
        return ""
    if len(parts) >= 2 and len(parts[0]) <= 2 and "." in parts[0]:
        surname = parts[-1].replace(".", "")
        initials = "".join(p.replace(".", "")[0] for p in parts[:-1] if p.replace(".", ""))
    else:
        surname = parts[0].replace(".", "")
        initials = "".join(p.replace(".", "")[0] for p in parts[1:] if p.replace(".", ""))
    return f"{surname} {initials}".strip()


# ============================================================
# GOOGLE SHEETS RETRY
# ============================================================

def sheets_call(operation, description="Google Sheets"):
    last_error = None
    for attempt in range(1, SHEETS_RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            retryable = any(k in text for k in (
                "429", "quota exceeded", "rate limit",
                "500", "502", "503", "504",
                "backend error", "service unavailable",
            ))
            if not retryable or attempt >= SHEETS_RETRY_ATTEMPTS:
                raise
            delay = SHEETS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            print(f"Google Sheets: временная ошибка ({exc}). "
                  f"Повтор {attempt}/{SHEETS_RETRY_ATTEMPTS} через {delay:.1f} сек.")
            sleep(delay)
    raise last_error


# ============================================================
# GOOGLE CLIENT
# ============================================================

def get_google_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    if not raw:
        raise RuntimeError("Не найден GOOGLE_SERVICE_ACCOUNT")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def open_tasks_sheet(client):
    """Всегда открывает Лист1 — рабочий лист с данными на сегодня/завтра."""
    book = client.open_by_key(TASKS_SPREADSHEET_ID)
    ws = book.worksheet(TASKS_SHEET_NAME)
    print(f"Использую вкладку: '{TASKS_SHEET_NAME}'")
    return ws, book


def open_doctors_sheet(client):
    return client.open_by_key(DOCTORS_SPREADSHEET_ID).worksheet(DOCTORS_SHEET_NAME)


# ============================================================
# ЖУРНАЛ BoTon_Log
# ============================================================

LOG_HEADERS = [
    "Timestamp", "Event", "BOTON-ID", "Дата", "Врач", "Email",
    "Mapping JSON", "Message-ID", "Result", "Notes",
]


def ensure_log_sheet(book):
    try:
        ws = book.worksheet(LOG_SHEET_NAME)
        print(f"BoTon_Log: {LOG_SHEET_NAME}")
        return ws
    except gspread.WorksheetNotFound:
        print(f"Google Sheets: создаю лист {LOG_SHEET_NAME}")
        ws = sheets_call(
            lambda: book.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_HEADERS)),
            "создание BoTon_Log",
        )
        sheets_call(
            lambda: ws.append_row(LOG_HEADERS, value_input_option="USER_ENTERED"),
            "заголовки BoTon_Log",
        )
        return ws


def make_log_row(event, bot_id="", target_date="", doctor="", email_address="",
                 mapping=None, message_id="", result="", notes=""):
    return [
        now_moscow().strftime("%d.%m.%Y %H:%M:%S"),
        event, bot_id, target_date, doctor, email_address,
        json.dumps(mapping or {}, ensure_ascii=False),
        message_id, result, notes,
    ]


def append_log_entries(log_sheet, entries):
    if not entries:
        return
    rows = []
    for e in entries:
        if isinstance(e, dict):
            rows.append(make_log_row(
                event=e.get("event", ""),
                bot_id=e.get("bot_id", ""),
                target_date=e.get("target_date", ""),
                doctor=e.get("doctor", ""),
                email_address=e.get("email_address", ""),
                mapping=e.get("mapping", {}),
                message_id=e.get("message_id", ""),
                result=e.get("result", ""),
                notes=e.get("notes", ""),
            ))
        else:
            rows.append(e)
    sheets_call(
        lambda: log_sheet.append_rows(rows, value_input_option="USER_ENTERED"),
        "пакетная запись BoTon_Log",
    )


def load_log_records(log_sheet):
    values = log_sheet.get_all_values()
    records = []
    for row in values[1:]:
        while len(row) < 10:
            row.append("")
        records.append({
            "timestamp": row[0], "event": row[1], "bot_id": row[2],
            "date": row[3], "doctor": row[4], "email": row[5],
            "mapping_json": row[6], "message_id": row[7],
            "result": row[8], "notes": row[9],
        })
    return records


def get_send_state(records, bot_id):
    for r in reversed(records):
        if r["event"] == "SEND" and r["bot_id"] == bot_id and r["result"] in ("SENT", "PENDING"):
            return r["result"]
    return ""


def find_log_by_bot_id(records, bot_id):
    for r in reversed(records):
        if r["event"] == "SEND" and r["bot_id"] == bot_id and r["result"] == "SENT":
            return r
    return None


def is_message_processed(records, message_id):
    if not message_id:
        return False
    return any(r["event"] == "RECEIVE" and r["message_id"] == message_id for r in records)


# ============================================================
# ВРАЧИ
# ============================================================

def load_doctors_map(doctors_sheet):
    values = doctors_sheet.get_all_values()
    result = []
    for row in values[1:]:
        if not row:
            continue
        name = str(row[0]).strip() if len(row) > 0 else ""
        chat_id = str(row[1]).strip() if len(row) > 1 else ""
        email_addr = str(row[2]).strip() if len(row) > 2 else ""
        if not name:
            continue
        result.append({"name": name, "chat_id": chat_id, "email": email_addr})
    print(f"Врачей в базе: {len(result)}")
    return result


def find_doctor_robust(task_name, doctors):
    sig = get_signature(task_name)
    if not sig:
        return None

    # 1) точное совпадение подписи
    for d in doctors:
        if get_signature(d["name"]) == sig:
            return d

    # 2) совпадение по фамилии + инициалам (частичное)
    sig_parts = sig.split()
    surname = sig_parts[0] if sig_parts else ""
    initials = sig_parts[1] if len(sig_parts) > 1 else ""
    for d in doctors:
        db_sig = get_signature(d["name"])
        db_parts = db_sig.split()
        db_sur = db_parts[0] if db_parts else ""
        db_ini = db_parts[1] if len(db_parts) > 1 else ""
        if db_sur == surname and surname:
            if not initials or not db_ini \
               or initials.startswith(db_ini) or db_ini.startswith(initials):
                return d

    # 3) подстрочное вхождение фамилии (>=4 символов)
    for d in doctors:
        db_sig = get_signature(d["name"])
        db_sur = db_sig.split()[0] if db_sig else ""
        if len(surname) >= 4 and len(db_sur) >= 4 \
           and (db_sur in surname or surname in db_sur):
            return d
    return None


# ============================================================
# ПАРСИНГ ЗАДАНИЙ
# ============================================================

def parse_date(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def is_branch_match(value):
    v = normalize_text(value)
    if not v:
        return False
    return any(k in v for k in BRANCH_KEYWORDS)


def get_tasks_from_row(row, row_number):
    tasks = []
    for task_col, status_col, number in (
        (COL_TASK1, COL_STATUS1, 1),
        (COL_TASK2, COL_STATUS2, 2),
        (COL_TASK3, COL_STATUS3, 3),
    ):
        value = row[task_col] if len(row) > task_col else ""
        if not str(value).strip():
            continue
        tasks.append({
            "task_number": number,
            "task_col": task_col,
            "status_col": status_col,
            "row_number": row_number,
            "title": TASK_TITLES.get(number, f"Задание {number}"),
            "text": str(value).strip(),
        })
    return tasks


def load_task_rows(tasks_sheet, target_date):
    values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение заданий")
    rows = []
    stats = {
        "total": 0, "empty": 0, "bad_date": 0, "other_date": 0,
        "other_branch": 0, "already_processed": 0, "no_doctor": 0,
        "no_tasks": 0, "selected": 0,
    }

    for row_number, row in enumerate(values[1:], start=2):
        stats["total"] += 1
        if not row or not any(str(x).strip() for x in row):
            stats["empty"] += 1
            continue

        row = list(row)
        while len(row) <= COL_COMPLETION:
            row.append("")

        date_value = parse_date(row[COL_DATE])
        if date_value is None:
            stats["bad_date"] += 1
            continue
        if date_value != target_date:
            stats["other_date"] += 1
            continue
        if not is_branch_match(row[COL_BRANCH]):
            stats["other_branch"] += 1
            continue

        completion = str(row[COL_COMPLETION]).strip()
        if not FORCE_RESEND and not TEST_TO_RESERVE:
            if completion and completion.lower() not in ("null", "undefined"):
                stats["already_processed"] += 1
                continue

        doctor_name = str(row[COL_DOCTOR]).strip()
        if not doctor_name or doctor_name.lower() == "нет данных":
            stats["no_doctor"] += 1
            continue

        tasks = get_tasks_from_row(row, row_number)
        if not tasks:
            stats["no_tasks"] += 1
            continue

        rows.append({
            "row_number": row_number,
            "doctor_name": doctor_name,
            "patient_id": str(row[COL_PATIENT_ID]).strip(),
            "patient_name": str(row[COL_PATIENT_NAME]).strip(),
            "tasks": tasks,
            "row": row,
        })
        stats["selected"] += 1

    return rows, stats


def print_stats(stats):
    print("\n--- СТАТИСТИКА ПРОВЕРКИ СТРОК ---")
    print(f"Всего строк в таблице: {stats['total']}")
    print(f"Пустых строк: {stats['empty']}")
    print(f"Нераспознанная дата: {stats['bad_date']}")
    print(f"Другая дата (не сегодня): {stats['other_date']}")
    print(f"Другой филиал: {stats['other_branch']}")
    print(f"Уже обработано (столбец M): {stats['already_processed']}")
    print(f"Нет ФИО врача: {stats['no_doctor']}")
    print(f"Нет заданий: {stats['no_tasks']}")
    print(f"ИТОГО ПОДОШЛО ДЛЯ ОТПРАВКИ: {stats['selected']}")
    print("-----------------------------------\n")


# ============================================================
# ГРУППИРОВКА И BOTON-ID
# ============================================================

def group_by_doctor(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[normalize_text(row["doctor_name"])].append(row)
    return grouped


def make_bot_id(target_date, doctor_signature, doctor_rows):
    parts = []
    for r in sorted(doctor_rows, key=lambda x: x["row_number"]):
        nums = ",".join(str(t["task_number"]) for t in r["tasks"])
        parts.append(f"{r['row_number']}:{nums}")
    signature = (
        f"{target_date.strftime('%Y%m%d')}|"
        f"{doctor_signature}|"
        f"{'/'.join(parts)}"
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10].upper()
    return f"BOTON-{target_date.strftime('%Y%m%d')}-{digest}"


def build_patients(doctor_rows):
    return [{
        "patient_name": r["patient_name"],
        "patient_id": r["patient_id"],
        "row_number": r["row_number"],
        "tasks": r["tasks"],
    } for r in doctor_rows]


def build_response_task_map(patients):
    mapping = defaultdict(list)
    for pnum, patient in enumerate(patients, start=1):
        tasks = patient["tasks"]
        for t_idx, task in enumerate(tasks):
            key = f"{pnum}" if len(tasks) == 1 else f"{pnum}{TASK_LETTERS[t_idx]}"
            mapping[key].append({
                "row_number": task["row_number"],
                "status_col": task["status_col"],
                "task_col": task["task_col"],
                "title": task["title"],
            })
    return dict(mapping)


# ============================================================
# ТЕКСТ ПИСЬМА ВРАЧУ
# ============================================================

def build_email_body(doctor, patients, bot_id):
    normalized = normalize_text(doctor["name"])
    parts = normalized.split()
    first = parts[1].capitalize() if len(parts) >= 2 else ""
    patr = parts[2].capitalize() if len(parts) >= 3 else ""
    if first and patr:
        address = f"{first} {patr}"
    elif first:
        address = first
    else:
        address = doctor["name"]

    lines = [
        f"Здравствуйте, {address}!",
        "",
        "Вам назначены задания по пациентам, записанным на сегодня:",
        "",
    ]
    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        lines.append(f"{pnum}. {p_name} — ОМС/ID: {p_id}")
        for t_idx, task in enumerate(patient["tasks"]):
            prefix = "" if len(patient["tasks"]) == 1 else f"{TASK_LETTERS[t_idx]}) "
            lines.append(f"   {prefix}{task['title']}")
            if task["text"]:
                lines.append(f"      {task['text']}")
        lines.append("")

    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    lines.extend([
        f"BOTON-ID: {bot_id}{resend_mark}",
        "",
        "Просьба вернуть отработанные задания заведующему отделением.",
        "",
        f"Отправлено: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК",
    ])
    return "\n".join(lines)


# ============================================================
# SMTP
# ============================================================

def send_email(to_email, subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)

    if not to_email:
        raise RuntimeError("Пустой получатель")

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, subtype="plain", charset="utf-8")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


# ============================================================
# ПАКЕТНАЯ ЗАПИСЬ СТАТУСОВ M / H-J-L
# ============================================================

def batch_update_cells(sheet, updates, description="пакетная запись"):
    if not updates:
        return
    data = []
    for item in updates:
        a1 = gspread.utils.rowcol_to_a1(int(item["row"]), int(item["col"]))
        data.append({"range": f"{a1}:{a1}", "values": [[item.get("value", "")]]})
    sheets_call(lambda: sheet.batch_update(data, value_input_option="USER_ENTERED"), description)


# ============================================================
# ДИАГНОСТИКА: ОДНО ПИСЬМО ЛЮБОГО ВРАЧА → TEST_EMAIL
# ============================================================

def send_doctor_diagnostic(rows, doctors, target_date):
    print("\n" + "=" * 60)
    print("ДИАГНОСТИКА: ОДНО ПИСЬМО ЛЮБОГО ВРАЧА → TEST_EMAIL")
    print("=" * 60)
    print(f"Получатель диагностики: {TEST_EMAIL}")

    if not rows:
        print("Нет подходящих строк для диагностики (см. статистику выше).")
        return False

    grouped = group_by_doctor(rows)
    doctor_key, doctor_rows = next(iter(grouped.items()))
    source_name = doctor_rows[0]["doctor_name"]
    doctor = find_doctor_robust(source_name, doctors)

    if not doctor:
        print(f"Врач '{source_name}' не найден в базе — диагностика невозможна.")
        return False

    patients = build_patients(doctor_rows)
    sig = get_signature(doctor["name"])
    bot_id = make_bot_id(target_date, sig, doctor_rows)
    body = build_email_body(doctor, patients, bot_id)

    print("\n--- ИНФОРМАЦИЯ О ПИСЬМЕ ---")
    print(f"Врач (из таблицы):  {source_name}")
    print(f"Врач (из базы):     {doctor['name']}")
    print(f"Email врача:        {doctor['email'] or 'НЕТ'}")
    print(f"Строк у врача:      {len(doctor_rows)}")
    print(f"Заданий:            {sum(len(r['tasks']) for r in doctor_rows)}")
    print(f"BOTON-ID:           {bot_id}")
    print(f"Дата:               {target_date.strftime('%d.%m.%Y')}")
    print("--- ТЕЛО ПИСЬМА ---")
    print(body)
    print("--- КОНЕЦ ТЕЛА ---\n")

    subject = (
        f"[ДИАГНОСТИКА BoTon] Для врача: {doctor['name']} — "
        f"{target_date.strftime('%d.%m.%Y')}"
    )

    try:
        send_email(TEST_EMAIL, subject, body)
        print("✅ ДИАГНОСТИЧЕСКОЕ ПИСЬМО ОТПРАВЛЕНО")
        print(f"   Получатель: {TEST_EMAIL}")
        print(f"   Тема: {subject}")
        return True
    except Exception as exc:
        print(f"❌ ОШИБКА ДИАГНОСТИЧЕСКОЙ ОТПРАВКИ: {exc}")
        return False


# ============================================================
# РАССЫЛКА
# ============================================================

def make_status_value(email_addr, is_resend, is_test, is_reserve):
    ts = now_moscow().strftime("%d.%m.%Y %H:%M")
    marks = []
    if is_test:
        marks.append("ТЕСТ")
    if is_resend:
        marks.append("ПОВТОРНО")
    mark = f" ({', '.join(marks)})" if marks else ""
    if is_reserve:
        return f"Отправлено{mark} на резервную почту {ts}"
    return f"Отправлено{mark} на {email_addr} {ts}"


def send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date):
    grouped = group_by_doctor(rows)
    records = load_log_records(log_sheet)

    prepared = []
    skipped_logged = 0
    not_found = 0

    for _, doctor_rows in grouped.items():
        source_name = doctor_rows[0]["doctor_name"]
        doctor = find_doctor_robust(source_name, doctors)
        if not doctor:
            print(f"Врач не найден в базе: {source_name}")
            not_found += 1
            continue

        patients = build_patients(doctor_rows)
        mapping = build_response_task_map(patients)
        sig = get_signature(doctor["name"])
        bot_id = make_bot_id(target_date, sig, doctor_rows)

        state = get_send_state(records, bot_id)
        if not TEST_TO_RESERVE:
            if state == "SENT" and not FORCE_RESEND:
                print(f"Пропуск (уже SENT): {bot_id}")
                skipped_logged += 1
                continue
            if state == "PENDING" and not FORCE_RESEND:
                print(f"Пропуск (PENDING): {bot_id}")
                skipped_logged += 1
                continue
            if state == "SENT" and FORCE_RESEND:
                print(f"ПОВТОРНАЯ ОТПРАВКА: {bot_id}")

        email_addr = normalize_email(doctor["email"])
        is_reserve = False
        if TEST_TO_RESERVE:
            email_addr = REPORT_EMAIL
            is_reserve = True
        elif not email_addr:
            email_addr = REPORT_EMAIL
            is_reserve = True

        resend_mark = " (ПОВТОРНО)" if FORCE_RESEND else ""
        test_mark = " [ТЕСТ]" if TEST_TO_RESERVE else ""
        if TEST_TO_RESERVE:
            subject = f"[ТЕСТ ВРАЧА: {doctor['name']}] Задания на {target_date.strftime('%d.%m.%Y')}{test_mark}"
        else:
            subject = f"Задания на {target_date.strftime('%d.%m.%Y')}{resend_mark} [BOTON-ID:{bot_id}]"

        body = build_email_body(doctor, patients, bot_id)

        prepared.append({
            "doctor": doctor,
            "doctor_rows": doctor_rows,
            "mapping": mapping,
            "bot_id": bot_id,
            "email": email_addr,
            "subject": subject,
            "body": body,
            "is_reserve": is_reserve,
        })

    # ========================================================
    # ТЕСТОВЫЙ РЕЖИМ: ОДНО СВОДНОЕ ПИСЬМО ВМЕСТО N ОТДЕЛЬНЫХ
    # ========================================================
    if TEST_TO_RESERVE:
        if not prepared:
            print("Тестовый режим: нечего отправлять")
            return {
                "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
                "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
            }

        print(f"\nТестовый режим: формирую ОДНО письмо со всеми {len(prepared)} письмами врачам")

        parts = [
            f"ТЕСТОВЫЙ ПРОГОН BoTon v{VERSION}",
            f"Дата: {target_date.strftime('%d.%m.%Y')}",
            f"Врачей в тесте: {len(prepared)}",
            f"Все задания идут на резервную почту, реальным врачам не отправляются.",
            "",
            "=" * 60,
            "",
        ]
        total_tasks = 0
        for idx, p in enumerate(prepared, start=1):
            total_tasks += sum(len(r["tasks"]) for r in p["doctor_rows"])
            parts.append(f"----- ПИСЬМО №{idx} ДЛЯ: {p['doctor']['name']} -----")
            parts.append(f"Кому (в боевом режиме): {p['doctor']['email'] or 'НЕТ EMAIL'}")
            parts.append(f"Тема: {p['subject']}")
            parts.append("")
            parts.append(p["body"])
            parts.append("")
            parts.append("=" * 60)
            parts.append("")

        combined_body = "\n".join(parts)
        combined_subject = (
            f"[ТЕСТ ВРАЧАМ] Сводка {len(prepared)} писем за "
            f"{target_date.strftime('%d.%m.%Y')} — BoTon v{VERSION}"
        )

        try:
            send_email(REPORT_EMAIL, combined_subject, combined_body)
            print(f"✅ ТЕСТОВОЕ СВОДНОЕ ПИСЬМО отправлено на {REPORT_EMAIL}")
            print(f"   Содержит {len(prepared)} писем врачам")
            return {
                "doctors": len(prepared),
                "rows": sum(len(p["doctor_rows"]) for p in prepared),
                "tasks": total_tasks,
                "errors": 0,
                "skipped": skipped_logged,
                "reserve": len(prepared),
                "not_found": not_found,
            }
        except Exception as exc:
            print(f"❌ ОШИБКА ТЕСТОВОЙ ОТПРАВКИ: {exc}")
            return {
                "doctors": 0, "rows": 0, "tasks": 0, "errors": 1,
                "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
            }

    # ========================================================
    # БОЕВОЙ РЕЖИМ
    # ========================================================
    if prepared:
        pending = [{
            "event": "SEND",
            "bot_id": p["bot_id"],
            "target_date": target_date.strftime("%d.%m.%Y"),
            "doctor": p["doctor"]["name"],
            "email_address": p["email"],
            "mapping": p["mapping"],
            "result": "PENDING",
            "notes": "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else "",
        } for p in prepared]
        print(f"BoTon_Log: пакетная запись PENDING ({len(pending)})")
        append_log_entries(log_sheet, pending)

    sent_logs = []
    error_logs = []
    m_updates = []

    successful_doctors = 0
    successful_rows = 0
    total_tasks = 0
    reserve_sent = 0
    errors = 0

    for p in prepared:
        doctor = p["doctor"]
        bot_id = p["bot_id"]
        email_addr = p["email"]
        is_reserve = p["is_reserve"]

        try:
            send_email(email_addr, p["subject"], p["body"])

            notes = "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else ""
            sent_logs.append({
                "event": "SEND",
                "bot_id": bot_id,
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": doctor["name"],
                "email_address": email_addr,
                "mapping": p["mapping"],
                "result": "SENT",
                "notes": notes,
            })
            status = make_status_value(email_addr, FORCE_RESEND, False, is_reserve)
            for r in p["doctor_rows"]:
                m_updates.append({
                    "row": r["row_number"],
                    "col": COL_COMPLETION + 1,
                    "value": status,
                })

            successful_doctors += 1
            successful_rows += len(p["doctor_rows"])
            total_tasks += sum(len(r["tasks"]) for r in p["doctor_rows"])
            if is_reserve:
                reserve_sent += 1

            # Пауза для снижения риска SMTP rate-limit
            sleep(2)

        except Exception as exc:
            errors += 1
            error_logs.append({
                "event": "SEND",
                "bot_id": bot_id,
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": doctor["name"],
                "email_address": email_addr,
                "mapping": p["mapping"],
                "result": "ERROR",
                "notes": str(exc),
            })
            print(f"Ошибка отправки {doctor['name']}: {exc}")

    if sent_logs or error_logs:
        print(f"BoTon_Log: пакетная запись SENT/ERROR ({len(sent_logs) + len(error_logs)})")
        append_log_entries(log_sheet, sent_logs + error_logs)
    if m_updates:
        print(f"Лист1: пакетная запись M ({len(m_updates)} строк)")
        batch_update_cells(tasks_sheet, m_updates, "запись статусов M")

    print("ИТОГ РАССЫЛКИ:")
    print(f"  Врачей обработано: {successful_doctors}")
    print(f"  Успешно отправлено: {successful_doctors - errors}")
    print(f"  Отправлено на резерв: {reserve_sent}")
    print(f"  Пропущено по журналу: {skipped_logged}")
    print(f"  Врач не найден: {not_found}")
    print(f"  Ошибок: {errors}")
    print(f"  Строк обновлено в M: {len(m_updates)}")
    print(f"  Всего заданий: {total_tasks}")

    return {
        "doctors": successful_doctors,
        "rows": len(m_updates),
        "tasks": total_tasks,
        "errors": errors,
        "skipped": skipped_logged,
        "reserve": reserve_sent,
        "not_found": not_found,
    }


# ============================================================
# IMAP — ПРИЁМ ОТВЕТОВ
# ============================================================

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


def parse_status(text):
    n = normalize_text(text)
    if "не выполнено" in n:
        return "❌ Не выполнено"
    if "частично" in n:
        return "⚠️ Частично"
    if "выполнено" in n:
        return "✅ Выполнено"
    return None


def expand_status_items(text, mapping):
    result = []
    normalized = normalize_text(text)
    pattern = re.compile(
        r"(?:выполнено|частично|не\s+выполнено)\s*[:\-]?\s*([0-9]+[А-В]?)",
        flags=re.IGNORECASE,
    )
    for key in pattern.findall(normalized):
        key = key.upper()
        if key in mapping:
            result.append(key)
    return result


STATUS_PRIORITY = {
    "": 0, "⏳ Нет ответа": 1,
    "✅ Выполнено": 2, "⚠️ Частично": 3, "❌ Не выполнено": 4,
}


def collect_status_update(updates, current_values, row_number, status_col, new_status):
    sheet_col = status_col + 1
    current = ""
    if 1 <= row_number <= len(current_values):
        r = current_values[row_number - 1]
        if sheet_col - 1 < len(r):
            current = str(r[sheet_col - 1] or "").strip()

    if STATUS_PRIORITY.get(new_status, 0) < STATUS_PRIORITY.get(current, 0):
        return
    key = (row_number, sheet_col)
    if STATUS_PRIORITY.get(updates.get(key, ""), 0) >= STATUS_PRIORITY.get(new_status, 0):
        return
    updates[key] = new_status


def process_incoming_messages(tasks_sheet, log_sheet, doctors):
    print("\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----")
    host = os.environ.get("IMAP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER", "")
    password = os.environ.get("IMAP_PASSWORD", "")

    doctor_emails = [normalize_email(d["email"]) for d in doctors if d["email"]]
    processed = 0
    mail = None

    try:
        print(f"IMAP: подключение к {host}:{port}...")
        mail = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT)
        mail.login(user, password)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            print("IMAP: не удалось открыть INBOX")
            return 0

        records = load_log_records(log_sheet)

        since_date = (now_moscow().date() - timedelta(days=IMAP_LOOKBACK_DAYS))
        since_imap = since_date.strftime("%d-%b-%Y")
        print(f"IMAP: ищем письма с {since_date.strftime('%d.%m.%Y')}")

        candidates = []
        seen = set()
        for addr in doctor_emails:
            if not addr:
                continue
            try:
                st, data = mail.search(None, "FROM", addr, "SINCE", since_imap)
                if st != "OK" or not data or not data[0]:
                    continue
                nums = data[0].split()[-IMAP_MAX_PER_DOCTOR:]
                for n in nums:
                    key = n.decode(errors="ignore")
                    if key not in seen:
                        seen.add(key)
                        candidates.append((n, addr))
            except Exception as exc:
                print(f"IMAP: ошибка поиска {addr}: {exc}")

        candidates = candidates[-IMAP_MAX_TOTAL:]
        print(f"IMAP: найдено потенциальных писем: {len(candidates)}")

        current_values = sheets_call(
            lambda: tasks_sheet.get_all_values(),
            "чтение Лист1 перед H/J/L",
        )

        status_updates = {}
        receive_logs = []

        for num, expected_email in candidates:
            try:
                st, data = mail.fetch(num, "(BODY.PEEK[])")
                if st != "OK" or not data:
                    continue
                raw = None
                for item in data:
                    if isinstance(item, tuple):
                        raw = item[1]
                        break
                if not raw:
                    continue

                msg = email.message_from_bytes(raw)
                message_id = (msg.get("Message-ID", "") or "").strip()

                if is_message_processed(records, message_id):
                    continue

                from_header = decode_mime_header(msg.get("From", ""))
                subject = decode_mime_header(msg.get("Subject", ""))
                body = extract_message_body(msg)

                m = re.search(r"<([^>]+)>", from_header)
                sender_email = normalize_email(m.group(1) if m else from_header)
                if sender_email != expected_email:
                    continue

                doctor = next(
                    (d for d in doctors if normalize_email(d["email"]) == sender_email),
                    None,
                )
                if not doctor:
                    continue

                bot_id = extract_bot_id(subject, body)
                if not bot_id:
                    continue

                log_record = find_log_by_bot_id(records, bot_id)
                if not log_record:
                    continue

                try:
                    mapping = json.loads(log_record["mapping_json"] or "{}")
                except Exception:
                    mapping = {}
                if not mapping:
                    continue

                text_all = f"{subject}\n{body}"
                status_value = parse_status(text_all)
                if not status_value:
                    continue

                keys = expand_status_items(text_all, mapping)
                if not keys:
                    continue

                updated = []
                for key in keys:
                    for item in mapping.get(key, []):
                        collect_status_update(
                            status_updates, current_values,
                            item["row_number"], item["status_col"], status_value,
                        )
                        updated.append({
                            "key": key,
                            "row": item["row_number"],
                            "title": item["title"],
                            "status": status_value,
                        })

                receive_logs.append({
                    "event": "RECEIVE",
                    "bot_id": bot_id,
                    "target_date": log_record["date"],
                    "doctor": doctor["name"],
                    "email_address": sender_email,
                    "mapping": mapping,
                    "message_id": message_id,
                    "result": "PROCESSED",
                    "notes": json.dumps(updated, ensure_ascii=False),
                })
                processed += 1
                print(f"Ответ обработан: {doctor['name']} [{bot_id}]")

            except Exception as exc:
                print(f"IMAP: ошибка обработки сообщения: {exc}")

        if status_updates:
            updates = [
                {"row": rn, "col": sc, "value": val}
                for (rn, sc), val in status_updates.items()
            ]
            print(f"Лист1: пакетная запись H/J/L ({len(updates)} ячеек)")
            batch_update_cells(tasks_sheet, updates, "запись H/J/L")

        if receive_logs:
            print(f"BoTon_Log: пакетная запись RECEIVE ({len(receive_logs)})")
            append_log_entries(log_sheet, receive_logs)

        return processed

    except Exception as exc:
        print(f"IMAP ERROR: {exc}")
        return processed
    finally:
        if mail is not None:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


# ============================================================
# ОТЧЁТ ЗАВЕДУЮЩЕМУ (PLAIN TEXT)
# ============================================================

def send_summary_report(target_date, send_stats, received_count, sheet_name):
    print("\n----- ФОРМИРОВАНИЕ И ОТПРАВКА ОТЧЁТА -----")
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    test_mark = " (ТЕСТОВЫЙ ПРОГОН)" if TEST_TO_RESERVE else ""

    text_body = (
        f"ОТЧЁТ СИСТЕМЫ BoTon v{VERSION}{resend_mark}{test_mark}\n"
        f"Дата формирования: {target_date.strftime('%d.%m.%Y %H:%M')} МСК\n"
        f"{'=' * 50}\n\n"
        f"ИСТОЧНИК ДАННЫХ:\n"
        f"  Вкладка: {sheet_name or 'НЕ НАЙДЕНА'}\n\n"
        f"СТАТИСТИКА РАССЫЛКИ:\n"
        f"  Врачей обработано: .............. {send_stats.get('doctors', 0)}\n"
        f"  Успешно отправлено: ............. "
        f"{send_stats.get('doctors', 0) - send_stats.get('errors', 0)}\n"
        f"  Отправлено на резерв: ........... {send_stats.get('reserve', 0)}\n"
        f"  Ошибок отправки: ................ {send_stats.get('errors', 0)}\n"
        f"  Пропущено по журналу: ........... {send_stats.get('skipped', 0)}\n"
        f"  Врач не найден в базе: .......... {send_stats.get('not_found', 0)}\n"
        f"  Строк обновлено в таблице (M): .. {send_stats.get('rows', 0)}\n"
        f"  Всего заданий в рассылке: ....... {send_stats.get('tasks', 0)}\n\n"
        f"ВХОДЯЩИЕ ОТВЕТЫ:\n"
        f"  Обработано ответов врачей: ...... {received_count}\n\n"
        f"{'=' * 50}\n"
        f"Система работает автоматически.\n"
        f"По вопросам — к администратору."
    )

    subject = (
        f"[ОТЧЁТ BoTon] Рассылка за {target_date.strftime('%d.%m.%Y')}"
        f"{resend_mark}{test_mark}"
    )

    try:
        send_email(REPORT_EMAIL, subject, text_body)
        print(f"ОТЧЁТ ОТПРАВЛЕН на {REPORT_EMAIL}")
    except Exception as exc:
        print(f"ОШИБКА ОТПРАВКИ ОТЧЁТА: {exc}")


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print(f"BoTon v{VERSION}")
    print(f"FORCE_RESEND = {FORCE_RESEND}")
    print(f"TEST_TO_RESERVE = {TEST_TO_RESERVE}")
    print(f"TEST_RECEIVE_ONLY = {TEST_RECEIVE_ONLY}")
    print(f"TEST_DOCTOR_DIAG = {TEST_DOCTOR_DIAG}")
    if TEST_DOCTOR_DIAG:
        print(f"TEST_EMAIL = {TEST_EMAIL}")
    print("=" * 60)

    target_date = now_moscow().date()
    print(f"Дата: {target_date.strftime('%d.%m.%Y')}")
    print(f"Время МСК: {now_moscow().strftime('%d.%m.%Y %H:%M:%S')}")

    client = get_google_client()
    print("Подключение к Google Sheets: OK")

    tasks_sheet, book = open_tasks_sheet(client)
    log_sheet = ensure_log_sheet(book)
    doctors_sheet = open_doctors_sheet(client)
    doctors = load_doctors_map(doctors_sheet)

    # ------------------- СБОР СТРОК -------------------
    rows, stats = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)

    # ------------------- ДИАГНОСТИКА -------------------
    if TEST_DOCTOR_DIAG:
        send_doctor_diagnostic(rows, doctors, target_date)
        print("\nРежим TEST_DOCTOR_DIAG=True — основная рассылка и приём ответов пропущены.")
        print(f"BoTon v{VERSION} завершён (диагностика).")
        return

    # ------------------- ПРИЁМ ОТВЕТОВ -------------------
    received = process_incoming_messages(tasks_sheet, log_sheet, doctors)

    # ------------------- РАССЫЛКА -------------------
    send_stats = {
        "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
        "skipped": 0, "reserve": 0, "not_found": 0,
    }

    if TEST_RECEIVE_ONLY:
        print("\n----- РАССЫЛКА ОТКЛЮЧЕНА (TEST_RECEIVE_ONLY=True) -----")
    elif not rows:
        print("\n----- РАССЫЛКА ПРОПУЩЕНА (0 подходящих строк на сегодня) -----")
    else:
        allowed = is_assignment_time() or FORCE_RESEND or TEST_TO_RESERVE
        if allowed:
            if TEST_TO_RESERVE:
                print("\n----- ТЕСТОВЫЙ ПРОГОН: СВОДНОЕ ПИСЬМО → НА РЕЗЕРВ -----")
            elif FORCE_RESEND:
                print("\n----- ПРИНУДИТЕЛЬНАЯ РАССЫЛКА (FORCE_RESEND) -----")
            else:
                print("\n----- ПЛАНОВАЯ РАССЫЛКА (окно 08:00–12:00 МСК) -----")
            send_stats = send_today_assignments(
                tasks_sheet, log_sheet, doctors, rows, target_date
            )
        else:
            print("\n----- РАССЫЛКА НЕ ВЫПОЛНЯЕТСЯ -----")
            print("Вне окна 08:00–12:00 МСК. FORCE_RESEND=False.")


    # ------------------- ОТЧЁТ -------------------
    # Отчёт отправляем только если было что отчитываться:
    #   - были отправки врачам (> 0),
    #   - или обработаны ответы врачей (> 0),
    #   - или это принудительный / диагностический прогон.
    has_send    = send_stats.get("doctors", 0) > 0
    has_receive = received > 0
    force_report = FORCE_RESEND or TEST_TO_RESERVE or TEST_DOCTOR_DIAG

    if has_send or has_receive or force_report:
        send_summary_report(target_date, send_stats, received, tasks_sheet.title)
    else:
        print("\nОтчёт не отправляется: 0 отправок и 0 ответов (пустой прогон).")

if __name__ == "__main__":
    main()
