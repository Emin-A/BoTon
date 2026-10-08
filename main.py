# ============================================================
# BoTon v3.1
# - Чтение данных ВСЕГДА из Лист1 (рабочий лист на сегодня/завтра)
# - Фильтр строк по столбцу A = сегодня МСК
# - Журнал BoTon_Log + BOTON-ID (защита от дублей)
# - IMAP-приём ответов врачей → H/J/L
# - Plain-text письма и отчёт (не HTML)
# - Retry Google Sheets (429/5xx)
# - Режимы: FORCE_RESEND, TEST_TO_RESERVE, TEST_RECEIVE_ONLY
# - Окно рассылки 08:00–12:00 МСК
#
# VERSION: 3.1
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

VERSION = "3.1"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ---------- РЕЖИМЫ ----------
FORCE_RESEND = False       # True = игнорировать журнал и время, отправить всё заново
TEST_TO_RESERVE = False    # True = все письма врачам → на REPORT_EMAIL (для проверки)
TEST_RECEIVE_ONLY = False  # True = только приём ответов, без рассылки

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

    # PENDING запись
    if not TEST_TO_RESERVE and prepared:
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

            if TEST_TO_RESERVE:
                print(f"ТЕСТОВОЕ ПИСЬМО → {REPORT_EMAIL} (для врача: {doctor['name']})")
            else:
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
                    m_updates.append({"row": r["row_number"], "col": COL_COMPLETION + 1, "value": status})

            successful_doctors += 1
            successful_rows += len(p["doctor_rows"])
            total_tasks += sum(len(r["tasks"]) for r in p["doctor_rows"])
            if is_reserve:
                reserve_sent += 1

        except Exception as exc:
            errors += 1
            if not TEST_TO_RESERVE:
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

    if not TEST_TO_RESERVE:
        final_logs = sent_logs + error_logs
        if final_logs:
            print(f"BoTon_Log: пакетная запись SENT/ERROR ({len(final_logs)})")
            append_log_entries(log_sheet, final_logs)
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
                print("\n----- ТЕСТОВЫЙ ПРОГОН: ПИСЬМА → НА РЕЗЕРВ -----")
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
    send_summary_report(target_date, send_stats, received, tasks_sheet.title)

    print(f"\nBoTon v{VERSION} завершён.")


if __name__ == "__main__":
    main()# ============================================================
# BoTon v2.8.2
# + ДЕТАЛЬНАЯ ДИАГНОСТИКА: точно покажет, почему строки пропускаются
# + Исправлена логика FORCE_RESEND и TEST_TO_RESERVE
#
# VERSION: 2.8.2
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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict
from datetime import datetime, timedelta, time
from time import sleep
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIG
# ============================================================

VERSION = "2.8.2"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

LOG_SHEET_NAME = "BoTon_Log"

# УПРАВЛЕНИЕ РАССЫЛКОЙ
# ВНИМАНИЕ: Для принудительной отправки установите FORCE_RESEND = True
FORCE_RESEND = True        
TEST_RECEIVE_ONLY = False  
TEST_TO_RESERVE = True     

REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"


# ============================================================
# GOOGLE SHEETS RETRY
# ============================================================

SHEETS_RETRY_ATTEMPTS = 5
SHEETS_RETRY_BASE_SECONDS = 1.5

def sheets_call(operation, description="Google Sheets"):
    last_error = None
    for attempt in range(1, SHEETS_RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            retryable = ("429" in text or "quota exceeded" in text or "rate limit" in text
                or "500" in text or "502" in text or "503" in text or "504" in text
                or "backend error" in text or "service unavailable" in text)
            if not retryable:
                raise
            if attempt >= SHEETS_RETRY_ATTEMPTS:
                raise
            delay = SHEETS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            print(f"Google Sheets: временная ошибка ({exc}). Повтор {attempt}/{SHEETS_RETRY_ATTEMPTS} через {delay:.1f} сек.")
            sleep(delay)
    raise last_error


# ============================================================
# IMAP
# ============================================================

IMAP_TIMEOUT = 20
IMAP_LOOKBACK_DAYS = 3
IMAP_MAX_PER_DOCTOR = 20
IMAP_MAX_TOTAL = 100

BRANCH_KEYWORDS = ["ф2", "филиал 2", "филиал № 2", "гп 107 ф 2", "гп 48", "107 ф 2", "гп 107 ф2"]
TASK_LETTERS = ["А", "Б", "В"]
TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}


def now_moscow():
    return datetime.now(MOSCOW_TZ)

def is_assignment_time():
    current_time = now_moscow().time()
    return time(8, 0) <= current_time <= time(12, 0)

def normalize_text(value):
    if value is None:
        return ""
    value = str(value).replace("Ё", "Е").replace("ё", "е").replace("\xa0", " ")
    value = re.sub(r"[^\w\sА-Яа-я]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()

def normalize_email(value):
    return str(value or "").strip().lower()


def get_google_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    if not raw:
        raise RuntimeError("Не найден GOOGLE_SERVICE_ACCOUNT")
    info = json.loads(raw)
    credentials = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(credentials)


def open_sheet(target_date=None):
    print("Подключение к Google Sheets...")
    client = get_google_client()
    tasks_book = client.open_by_key(TASKS_SPREADSHEET_ID)

    # Показываем все доступные вкладки — для диагностики
    all_sheets = [s.title for s in tasks_book.worksheets()]
    print(f"Доступные вкладки: {all_sheets}")

    # Ищем вкладку с именем = сегодняшняя дата (формат ДД.ММ.ГГГГ)
    tasks_sheet = None
    if target_date:
        date_sheet_name = target_date.strftime("%d.%m.%Y")
        if date_sheet_name in all_sheets:
            tasks_sheet = tasks_book.worksheet(date_sheet_name)
            print(f"Найдена вкладка с данными за сегодня: '{date_sheet_name}'")
        else:
            print(f"Вкладка '{date_sheet_name}' не найдена. "
                  f"Данных за сегодня в таблице нет.")

    # Если вкладки с датой нет — берём Лист1 (шаблон)
    if tasks_sheet is None:
        try:
            tasks_sheet = tasks_book.worksheet("Лист1")
            print("Используется вкладка 'Лист1' (шаблон, данных может не быть)")
        except gspread.WorksheetNotFound:
            raise RuntimeError("В таблице нет ни вкладки с датой, ни 'Лист1'")

    doctors_book = client.open_by_key(DOCTORS_SPREADSHEET_ID)
    doctors_sheet = doctors_book.worksheet(DOCTORS_SHEET_NAME)
    print("Google Sheets: OK")
    return tasks_sheet, doctors_sheet, tasks_book


LOG_HEADERS = ["Timestamp", "Event", "BOTON-ID", "Дата", "Врач", "Email", "Mapping JSON", "Message-ID", "Result", "Notes"]

def ensure_log_sheet(tasks_book):
    try:
        log_sheet = tasks_book.worksheet(LOG_SHEET_NAME)
        return log_sheet
    except gspread.WorksheetNotFound:
        log_sheet = sheets_call(lambda: tasks_book.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_HEADERS)), "создание BoTon_Log")
        sheets_call(lambda: log_sheet.append_row(LOG_HEADERS, value_input_option="USER_ENTERED"), "запись заголовков")
        return log_sheet

def make_log_row(event, bot_id="", target_date="", doctor="", email_address="", mapping=None, message_id="", result="", notes=""):
    return [now_moscow().strftime("%d.%m.%Y %H:%M:%S"), event, bot_id, target_date, doctor, email_address,
            json.dumps(mapping or {}, ensure_ascii=False), message_id, result, notes]

def append_log_entries(log_sheet, entries):
    if not entries:
        return
    rows = [make_log_row(**entry) if isinstance(entry, dict) else entry for entry in entries]
    sheets_call(lambda: log_sheet.append_rows(rows, value_input_option="USER_ENTERED"), "пакетная запись BoTon_Log")


def load_doctors(doctors_sheet):
    doctors = []
    for row in doctors_sheet.get_all_values()[1:]:
        if not row or not row[0].strip():
            continue
        doctors.append({"name": row[0].strip(), "chat_id": row[1].strip() if len(row) > 1 else "", "email": row[2].strip() if len(row) > 2 else ""})
    print(f"Врачей в базе: {len(doctors)}")
    return doctors


def extract_surname(value):
    parts = normalize_text(value).split()
    return parts[0] if parts else ""

def extract_initials(value):
    parts = normalize_text(value).split()
    return "".join([part[0] for part in parts[1:] if part])

def make_doctor_key(value):
    return re.sub(r"\s+", "", normalize_text(value))

def doctor_matches(doctor_name, task_doctor):
    a, b = normalize_text(doctor_name), normalize_text(task_doctor)
    if not a or not b: return False
    if a == b: return True
    sa, sb = extract_surname(a), extract_surname(b)
    if sa and sb and sa == sb:
        ia, ib = extract_initials(a), extract_initials(b)
        if not ia or not ib or ia.startswith(ib) or ib.startswith(ia): return True
    if len(sa) >= 4 and sa in b: return True
    if len(sb) >= 4 and sb in a: return True
    return False

def find_doctor(task_doctor, doctors):
    for doctor in doctors:
        if doctor_matches(doctor["name"], task_doctor):
            return doctor
    return None

def is_branch_2(value):
    val = normalize_text(value)
    return any(keyword in val for keyword in BRANCH_KEYWORDS) if val else False

def parse_date(value):
    if not value: return None
    for fmt in ["%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"]:
        try: return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError: pass
    return None


def get_task_from_row(row, row_number):
    tasks = []
    for task_col, status_col, task_number in [(6, 7, 1), (8, 9, 2), (10, 11, 3)]:
        task_value = row[task_col] if len(row) > task_col else ""
        if str(task_value).strip():
            tasks.append({"task_col": task_col, "status_col": status_col, "task_number": task_number,
                          "title": TASK_TITLES.get(task_number, f"Задание {task_number}"), "text": str(task_value).strip(), "row_number": row_number})
    return tasks


def load_task_rows(tasks_sheet, target_date):
    values = tasks_sheet.get_all_values()
    rows = []
    stats = {"total": 0, "empty": 0, "bad_date": 0, "other_date": 0, "other_branch": 0, "already_processed": 0, "no_doctor": 0, "no_tasks": 0, "selected": 0}
    
    for row_number, row in enumerate(values[1:], start=2):
        stats["total"] += 1
        if not row or not any(str(x).strip() for x in row):
            stats["empty"] += 1
            continue
        
        row = list(row)
        while len(row) < 13:
            row.append("")

        date_value = parse_date(row[0])
        if date_value is None:
            stats["bad_date"] += 1
            continue
        
        # ВАЖНО: Даже при FORCE_RESEND мы проверяем дату, чтобы не отправить задания за прошлые месяцы
        if date_value != target_date:
            stats["other_date"] += 1
            continue
        
        if not is_branch_2(row[1]):
            stats["other_branch"] += 1
            continue
        
        # Проверка столбца M (индекс 12). Игнорируется, если FORCE_RESEND или TEST_TO_RESERVE
        if str(row[12]).strip() and not FORCE_RESEND and not TEST_TO_RESERVE:
            stats["already_processed"] += 1
            continue

        doctor_name = str(row[2]).strip()
        if not doctor_name:
            stats["no_doctor"] += 1
            continue

        tasks = get_task_from_row(row, row_number)
        if not tasks:
            stats["no_tasks"] += 1
            continue

        rows.append({"row_number": row_number, "date": date_value, "branch": row[1], "doctor_name": doctor_name,
                     "patient_id": row[3], "patient_name": row[4], "source": row[5], "tasks": tasks, "row": row})
        stats["selected"] += 1

    # ДЕТАЛЬНАЯ ДИАГНОСТИКА
    print("\n--- СТАТИСТИКА ПРОВЕРКИ СТРОК ---")
    print(f"Всего строк в таблице: {stats['total']}")
    print(f"Пустых строк: {stats['empty']}")
    print(f"Нераспознанная дата: {stats['bad_date']}")
    print(f"Другая дата (не сегодня): {stats['other_date']}  <-- ПРОВЕРЬТЕ ЭТО!")
    print(f"Другой филиал: {stats['other_branch']}")
    print(f"Уже обработано (столбец M): {stats['already_processed']}")
    print(f"Нет ФИО врача: {stats['no_doctor']}")
    print(f"Нет заданий: {stats['no_tasks']}")
    print(f"ИТОГО ПОДОШЛО ДЛЯ ОТПРАВКИ: {stats['selected']}")
    print("-----------------------------------\n")
    
    return rows, stats


def group_by_doctor(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[normalize_text(row["doctor_name"])].append(row)
    return grouped

def build_patients(rows):
    return [{"patient_name": row["patient_name"], "patient_id": row["patient_id"], "row_number": row["row_number"], "tasks": row["tasks"]} for row in rows]

def make_bot_id(target_date, doctor, doctor_rows):
    parts = [f"{row['row_number']}:{','.join(str(t['task_number']) for t in row['tasks'])}" for row in sorted(doctor_rows, key=lambda x: x["row_number"])]
    signature = f"{target_date.strftime('%Y%m%d')}|{make_doctor_key(doctor)}|{'/'.join(parts)}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10].upper()
    return f"BOTON-{target_date.strftime('%Y%m%d')}-{make_doctor_key(doctor)}-{digest}"

def build_exact_response_task_map(patients):
    mapping = defaultdict(list)
    for p_num, patient in enumerate(patients, start=1):
        for t_idx, task in enumerate(patient["tasks"]):
            key = f"{p_num}" if len(patient["tasks"]) == 1 else f"{p_num}{TASK_LETTERS[t_idx]}"
            mapping[key].append({"row_number": task["row_number"], "status_col": task["status_col"], "task_col": task["task_col"], "title": task["title"]})
    return dict(mapping)

def build_email_body(doctor, patients, bot_id):
    parts = normalize_text(doctor["name"]).split()
    first_name = parts[1] if len(parts) >= 2 else ""
    patronymic = parts[2] if len(parts) >= 3 else ""
    address_name = f"{first_name.capitalize()} {patronymic.capitalize()}" if first_name and patronymic else (first_name.capitalize() if first_name else doctor["name"])

    lines = [f"Уважаемая(ый) {address_name}!", "", "Вам необходимо выполнить важные задания по записанным на сегодня к вам пациентам:", ""]
    for p_num, patient in enumerate(patients, start=1):
        lines.append(f"{p_num}. {patient['patient_name'] or 'ФИО не указано'} — {patient['patient_id'] or 'не указан'}")
        for t_idx, task in enumerate(patient["tasks"]):
            prefix = "" if len(patient["tasks"]) == 1 else f"{TASK_LETTERS[t_idx]}) "
            lines.append(f"   {prefix}{task['title']}")
            if task["text"]: lines.append(f"      {task['text']}")
        lines.append("")

    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    lines.extend([f"BOTON-ID: {bot_id}{resend_mark}", "Срок выполнения: сегодня.", "", "Прошу вернуть отработанное задание вашему заведующему.", "С уважением,", "BoTon и Агаввердиев Э.М.", now_moscow().strftime("%d.%m.%Y %H:%M:%S")])
    return "\n".join(lines)

def send_email(recipient, subject, body):
    host, port = os.environ.get("SMTP_HOST", ""), int(os.environ.get("SMTP_PORT", "465"))
    username, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM_EMAIL", username)
    if not recipient: raise RuntimeError("У врача отсутствует email")

    message = MIMEMultipart()
    message["From"], message["To"], message["Subject"] = from_email, recipient, subject
    message.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as server:
        server.login(username, password)
        server.sendmail(from_email, [recipient], message.as_string())

def batch_update_cells(sheet, updates, description="пакетное обновление"):
    if not updates: return
    data = [{"range": f"{gspread.utils.rowcol_to_a1(int(item['row']), int(item['col']))}:{gspread.utils.rowcol_to_a1(int(item['row']), int(item['col']))}", "values": [[item.get("value", "")]]} for item in updates]
    sheets_call(lambda: sheet.batch_update(data, value_input_option="USER_ENTERED"), description)

def load_log_records(log_sheet):
    return [{"timestamp": row[0], "event": row[1], "bot_id": row[2], "date": row[3], "doctor": row[4], "email": row[5], "mapping_json": row[6], "message_id": row[7], "result": row[8], "notes": row[9]} for row in [r + [""]*(10-len(r)) for r in log_sheet.get_all_values()[1:]]]

def get_assignment_log_state(records, bot_id):
    for record in reversed(records):
        if record["event"] == "SEND" and record["bot_id"] == bot_id:
            return record["result"]
    return ""

def find_log_by_bot_id(records, bot_id):
    for record in reversed(records):
        if record["bot_id"] == bot_id and record["event"] == "SEND" and record["result"] == "SENT":
            return record
    return None

def is_message_processed(records, message_id):
    return any(record["event"] == "RECEIVE" and record["message_id"] == message_id for record in records) if message_id else False

def reconcile_sent_rows(tasks_sheet, records):
    sheet_values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение Лист1")
    current_m = {row_number: str(row[12]).strip() if len(row) > 12 else "" for row_number, row in enumerate(sheet_values[1:], start=2)}
    updates, seen_rows = [], set()
    
    for record in records:
        if record["event"] != "SEND" or record["result"] != "SENT" or not record["mapping_json"]: continue
        try: mapping = json.loads(record["mapping_json"])
        except: continue
        if not record["bot_id"]: continue
        
        resend_mark = " (ПОВТОРНО)" if "ПОВТОР" in record.get("notes", "") else ""
        value = f"Отправлено{resend_mark} {record['timestamp']} [{record['bot_id']}]"
        
        for items in mapping.values():
            for item in items:
                rn = int(item["row_number"])
                if rn not in seen_rows and not current_m.get(rn, ""):
                    seen_rows.add(rn)
                    updates.append({"row": rn, "col": 13, "value": value})

    if updates:
        print(f"Восстановление M по BoTon_Log: {len(updates)} строк")
        batch_update_cells(tasks_sheet, updates, "восстановление M")


def send_today_assignments(tasks_sheet, log_sheet, doctors_map, rows, target_date):
    grouped = group_by_doctor(rows)
    successful_doctors, successful_rows, total_tasks, reserve_count, errors, skipped_logged = 0, 0, 0, 0, 0, 0
    records = load_log_records(log_sheet)
    
    if not TEST_TO_RESERVE:
        reconcile_sent_rows(tasks_sheet, records)

    prepared = []
    for doctor_key, doctor_rows in grouped.items():
        doctor = find_doctor(doctor_rows[0]["doctor_name"], doctors_map)
        if not doctor:
            print(f"Врач не найден: {doctor_rows[0]['doctor_name']}")
            errors += 1
            continue

        patients = build_patients(doctor_rows)
        mapping = build_exact_response_task_map(patients)
        bot_id = make_bot_id(target_date, doctor["name"], doctor_rows)
        state = get_assignment_log_state(records, bot_id)

        if not TEST_TO_RESERVE:
            if state == "SENT" and not FORCE_RESEND:
                print(f"Пропуск: BOTON-ID уже отправлен {bot_id}")
                skipped_logged += 1
                continue
            elif state == "SENT" and FORCE_RESEND:
                print(f"ПОВТОРНАЯ ОТПРАВКА: BOTON-ID {bot_id} (FORCE_RESEND=True)")
            elif state == "PENDING" and not FORCE_RESEND:
                print(f"Пропуск: BOTON-ID в состоянии PENDING: {bot_id}")
                skipped_logged += 1
                continue

        email_address = normalize_email(doctor["email"])
        is_reserve = False
        if TEST_TO_RESERVE:
            email_address = REPORT_EMAIL
            is_reserve = True
        elif not email_address:
            email_address = REPORT_EMAIL
            reserve_count += 1
            is_reserve = True

        resend_mark = " (ПОВТОРНО)" if FORCE_RESEND else ""
        subject = f"[ТЕСТ] Для врача: {doctor['name']} [BOTON-ID:{bot_id}]" if TEST_TO_RESERVE else f"Задания на сегодня{resend_mark} [BOTON-ID:{bot_id}]"
        body = build_email_body(doctor, patients, bot_id)

        prepared.append({"doctor": doctor, "doctor_rows": doctor_rows, "patients": patients, "mapping": mapping, "bot_id": bot_id, "email_address": email_address, "subject": subject, "body": body, "target_date": target_date.strftime("%d.%m.%Y"), "is_reserve": is_reserve})

    if not TEST_TO_RESERVE:
        pending_entries = [{"event": "SEND", "bot_id": item["bot_id"], "target_date": item["target_date"], "doctor": item["doctor"]["name"], "email_address": item["email_address"], "mapping": item["mapping"], "result": "PENDING", "notes": "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else ""} for item in prepared]
        if pending_entries:
            print(f"BoTon_Log: пакетная запись PENDING ({len(pending_entries)} врачей)")
            append_log_entries(log_sheet, pending_entries)

    sent_log_entries, error_log_entries, m_updates = [], [], []
    for item in prepared:
        try:
            send_email(item["email_address"], item["subject"], item["body"])
            if TEST_TO_RESERVE:
                print(f"ТЕСТОВОЕ ПИСЬМО отправлено на {REPORT_EMAIL} (для врача: {item['doctor']['name']})")
            else:
                sent_log_entries.append({"event": "SEND", "bot_id": item["bot_id"], "target_date": item["target_date"], "doctor": item["doctor"]["name"], "email_address": item["email_address"], "mapping": item["mapping"], "result": "SENT", "notes": "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else ""})
                timestamp = now_moscow().strftime("%d.%m.%Y %H:%M:%S")
                resend_mark = " (ПОВТОРНО)" if FORCE_RESEND else ""
                for row in item["doctor_rows"]:
                    m_updates.append({"row": row["row_number"], "col": 13, "value": f"Отправлено{resend_mark} {timestamp} [{item['bot_id']}]"})
                print(f"Письмо врачу отправлено: {item['doctor']['name']} -> {item['email_address']}")
            
            successful_doctors += 1
            successful_rows += len(item["doctor_rows"])
            total_tasks += sum(len(t["tasks"]) for t in item["doctor_rows"])
        except Exception as exc:
            errors += 1
            if not TEST_TO_RESERVE:
                error_log_entries.append({"event": "SEND", "bot_id": item["bot_id"], "target_date": item["target_date"], "doctor": item["doctor"]["name"], "email_address": item["email_address"], "mapping": item["mapping"], "result": "ERROR", "notes": str(exc)})
            print(f"Ошибка отправки {item['doctor']['name']}: {exc}")

    if not TEST_TO_RESERVE:
        if sent_log_entries or error_log_entries:
            print(f"BoTon_Log: пакетная запись SENT/ERROR ({len(sent_log_entries) + len(error_log_entries)})")
            append_log_entries(log_sheet, sent_log_entries + error_log_entries)
        if m_updates:
            print(f"Лист1: пакетная запись M ({len(m_updates)} строк)")
            batch_update_cells(tasks_sheet, m_updates, "пакетная запись M")

    print("\n--- ИТОГ РАССЫЛКИ ---")
    print(f"Врачей обработано: {len(grouped)}")
    print(f"Успешно отправлено: {successful_doctors}")
    if not TEST_TO_RESERVE: print(f"Пропущено по журналу: {skipped_logged}")
    print(f"Ошибок: {errors}")
    print(f"Строк записано в M: {successful_rows}")
    print(f"Всего заданий: {total_tasks}")
    print("---------------------")

    return {"doctors": successful_doctors, "rows": successful_rows, "tasks": total_tasks, "errors": errors, "skipped": skipped_logged, "reserve": reserve_count}


# ============================================================
# IMAP & REPORT (сокращено для экономии места, логика идентична v2.8.1)
# ============================================================

def decode_mime_header(value):
    if not value: return ""
    result = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            try: result.append(part.decode(encoding or "utf-8", errors="replace"))
            except: result.append(part.decode("utf-8", errors="replace"))
        else: result.append(str(part))
    return "".join(result)

def extract_message_body(message):
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")).lower():
                payload = part.get_payload(decode=True)
                if payload: chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        return "\n".join(chunks)
    payload = message.get_payload(decode=True)
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace") if payload else ""

def search_doctor_messages(mail, doctor_emails):
    since_imap = (now_moscow().date() - timedelta(days=IMAP_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    found, seen = [], set()
    for email_address in [normalize_email(e) for e in doctor_emails if e]:
        try:
            status, data = mail.search(None, "FROM", email_address, "SINCE", since_imap)
            if status == "OK" and data and data[0]:
                for num in data[0].split()[-IMAP_MAX_PER_DOCTOR:]:
                    key = num.decode(errors="ignore")
                    if key not in seen:
                        seen.add(key)
                        found.append((num, email_address))
        except Exception as exc: print(f"IMAP ошибка поиска {email_address}: {exc}")
    return found[-IMAP_MAX_TOTAL:]

def extract_bot_id(subject, body):
    match = re.search(r"BOTON-ID\s*[:\-]?\s*(BOTON-[A-Za-z0-9А-Яа-я_.-]+)", f"{subject}\n{body}", flags=re.IGNORECASE)
    return match.group(1) if match else ""

def parse_status(text):
    norm = normalize_text(text)
    if "не выполнено" in norm: return "❌ Не выполнено"
    if "частично" in norm: return "⚠️ Частично"
    if "выполнено" in norm: return "✅ Выполнено"
    return None

def expand_status_items(text, mapping):
    result = []
    for key in re.findall(r"(?:выполнено|частично|не\s+выполнено)\s*[:\-]?\s*([0-9]+[А-В]?)", normalize_text(text), flags=re.IGNORECASE):
        if key.upper() in mapping: result.append(key.upper())
    return result

STATUS_PRIORITY = {"": 0, "⏳ Нет ответа": 1, "✅ Выполнено": 2, "⚠️ Частично": 3, "❌ Не выполнено": 4}

def collect_status_update(updates, current_values, row_number, status_col, new_status):
    sheet_col = status_col + 1
    current = str(current_values[row_number - 1][sheet_col - 1] or "").strip() if row_number >= 1 and row_number - 1 < len(current_values) and sheet_col - 1 < len(current_values[row_number - 1]) else ""
    if STATUS_PRIORITY.get(new_status, 0) >= STATUS_PRIORITY.get(current, 0):
        old_update = updates.get((row_number, sheet_col))
        if not old_update or STATUS_PRIORITY.get(new_status, 0) > STATUS_PRIORITY.get(old_update, 0):
            updates[(row_number, sheet_col)] = new_status

def process_incoming_messages(tasks_sheet, log_sheet, doctors):
    print("\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----")
    host, port = os.environ.get("IMAP_HOST", "imap.yandex.ru"), int(os.environ.get("IMAP_PORT", "993"))
    username, password = os.environ.get("IMAP_USER", ""), os.environ.get("IMAP_PASSWORD", "")
    doctor_emails = [normalize_email(doctor["email"]) for doctor in doctors if doctor["email"]]
    mail, processed = None, 0
    try:
        mail = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT)
        mail.login(username, password)
        if mail.select("INBOX", readonly=True)[0] != "OK": return 0
        
        records = load_log_records(log_sheet)
        candidates = search_doctor_messages(mail, doctor_emails)
        current_values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение Лист1")
        status_updates, receive_logs = {}, []

        for message_number, expected_email in candidates:
            try:
                status, data = mail.fetch(message_number, "(BODY.PEEK[])")
                if status != "OK" or not data: continue
                raw_message = next((item[1] for item in data if isinstance(item, tuple)), None)
                if not raw_message: continue
                
                message = email.message_from_bytes(raw_message)
                message_id = (message.get("Message-ID", "") or "").strip()
                if is_message_processed(records, message_id): continue

                sender_email = normalize_email(re.search(r"<([^>]+)>", decode_mime_header(message.get("From", ""))).group(1) if re.search(r"<([^>]+)>", decode_mime_header(message.get("From", ""))) else decode_mime_header(message.get("From", "")))
                if sender_email != expected_email: continue

                doctor = next((item for item in doctors if normalize_email(item["email"]) == sender_email), None)
                if not doctor: continue

                bot_id = extract_bot_id(decode_mime_header(message.get("Subject", "")), extract_message_body(message))
                log_record = find_log_by_bot_id(records, bot_id)
                if not bot_id or not log_record: continue

                try: mapping = json.loads(log_record["mapping_json"] or "{}")
                except: mapping = {}
                if not mapping: continue

                status_value = parse_status(f"{decode_mime_header(message.get('Subject', ''))}\n{extract_message_body(message)}")
                if not status_value: continue

                keys = expand_status_items(f"{decode_mime_header(message.get('Subject', ''))}\n{extract_message_body(message)}", mapping)
                if not keys: continue

                updated = []
                for key in keys:
                    for item in mapping.get(key, []):
                        collect_status_update(status_updates, current_values, item["row_number"], item["status_col"], status_value)
                        updated.append({"key": key, "row": item["row_number"], "status_col": item["status_col"], "title": item["title"], "status": status_value})

                receive_logs.append({"event": "RECEIVE", "bot_id": bot_id, "target_date": log_record["date"], "doctor": doctor["name"], "email_address": sender_email, "mapping": mapping, "message_id": message_id, "result": "PROCESSED", "notes": json.dumps(updated, ensure_ascii=False)})
                processed += 1
                print(f"Ответ подготовлен: {doctor['name']} [{bot_id}]")
            except Exception as exc: print(f"IMAP ошибка обработки: {exc}")

        if status_updates:
            batch_update_cells(tasks_sheet, [{"row": rn, "col": sc, "value": val} for (rn, sc), val in status_updates.items()], "пакетная запись H/J/L")
        if receive_logs:
            append_log_entries(log_sheet, receive_logs)
        return processed
    except Exception as exc: print(f"IMAP ERROR: {exc}"); return 0
    finally:
        if mail:
            try: mail.close(); mail.logout()
            except: pass

def send_summary_report(target_date, send_stats, received_count):
    print("\n----- ФОРМИРОВАНИЕ И ОТПРАВКА ОТЧЕТА -----")
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    test_mark = " (ТЕСТОВЫЙ ПРОГОН)" if TEST_TO_RESERVE else ""
    text_body = (f"ОТЧЕТ СИСТЕМЫ BoTon v{VERSION}{resend_mark}{test_mark}\nДата: {target_date.strftime('%d.%m.%Y %H:%M')}\n{'='*45}\n\n"
                 f"СТАТИСТИКА РАССЫЛКИ:\n  - Врачей обработано: {send_stats['doctors']}\n  - Успешно: {send_stats['doctors'] - send_stats['errors']}\n"
                 f"  - Тестовых/Резервных: {send_stats['doctors'] if TEST_TO_RESERVE else send_stats['reserve']}\n  - Ошибок: {send_stats['errors']}\n"
                 f"  - Строк в M: {send_stats['rows']}\n  - Заданий: {send_stats['tasks']}\n\nВХОДЯЩИЕ ОТВЕТЫ: {received_count}\n{'='*45}")
    try:
        send_email(REPORT_EMAIL, f"[ОТЧЕТ BoTon] {target_date.strftime('%d.%m.%Y')}{resend_mark}{test_mark}", text_body)
        print(f"ОТЧЕТ ОТПРАВЛЕН на {REPORT_EMAIL}")
    except Exception as exc: print(f"ОШИБКА ОТЧЕТА: {exc}")


def main():
    print(f"BOTON v{VERSION} | FORCE_RESEND={FORCE_RESEND} | TEST_TO_RESERVE={TEST_TO_RESERVE}")
    
    # 1. Определяем дату
    target_date = now_moscow().date()
    print(f"Дата: {target_date.strftime('%d.%m.%Y')} | Время МСК: {now_moscow().strftime('%H:%M:%S')}")

    # 2. Подключаемся к таблицам (с автоматическим поиском вкладки по дате)
    tasks_sheet, doctors_sheet, tasks_book = open_sheet(target_date)
    
    # 3. Создаём или получаем лист журнала BoTon_Log
    log_sheet = ensure_log_sheet(tasks_book)

    # 4. Загружаем базу врачей
    doctors = load_doctors(doctors_sheet)

    # 5. Загружаем задания из таблицы
    rows, stats = load_task_rows(tasks_sheet, target_date)

    # 6. Проверяем входящие ответы (IMAP)
    received = process_incoming_messages(tasks_sheet, log_sheet, doctors)
    print(f"Обработано входящих: {received}")

    # 7. Рассылка заданий
    send_stats = {"doctors": 0, "rows": 0, "tasks": 0, "errors": 0, "skipped": 0, "reserve": 0}
    if not TEST_RECEIVE_ONLY:
        if is_assignment_time() or FORCE_RESEND or TEST_TO_RESERVE:
            send_stats = send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date)
        else:
            print("\n----- РАССЫЛКА НЕ ВЫПОЛНЯЕТСЯ (вне 08:00–12:00) -----")

    # 8. Отправка отчёта заведующему
    send_summary_report(target_date, send_stats, received)
    
    print(f"\nBOTON v{VERSION} завершён.")

if __name__ == "__main__":
    main()
