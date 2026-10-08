# ============================================================
# BoTon v2.8
# + РЕЖИМ TEST_TO_RESERVE: отправка реальных писем врачам на резервную почту
#
# VERSION: 2.8
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

VERSION = "2.8"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

LOG_SHEET_NAME = "BoTon_Log"

# УПРАВЛЕНИЕ РАССЫЛКОЙ
FORCE_RESEND = False  # True = принудительно отправить всё заново (игнорирует журнал и время)
TEST_MODE = False  # True = отправить одно тестовое письмо со статистикой
TEST_RECEIVE_ONLY = False  # True = только принимать ответы, не рассылать
TEST_TO_RESERVE = True  # True = отправить реальные письма врачам на REPORT_EMAIL (для проверки)

REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"  # Почта заведующего и резервная почта


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
            retryable = (
                "429" in text or "quota exceeded" in text or "rate limit" in text
                or "500" in text or "502" in text or "503" in text or "504" in text
                or "backend error" in text or "service unavailable" in text
            )
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


# ============================================================
# BRANCH
# ============================================================

BRANCH_KEYWORDS = [
    "ф2", "филиал 2", "филиал № 2", "гп 107 ф 2", "гп 48", "107 ф 2", "гп 107 ф2",
]

TASK_LETTERS = ["А", "Б", "В"]

TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}


# ============================================================
# TIME
# ============================================================

def now_moscow():
    return datetime.now(MOSCOW_TZ)


def today_str():
    return now_moscow().strftime("%d.%m.%Y")


def is_assignment_time():
    current_time = now_moscow().time()
    return time(8, 0) <= current_time <= time(12, 0)


# ============================================================
# NORMALIZATION
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


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_google_client():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    if not raw:
        raise RuntimeError("Не найден GOOGLE_SERVICE_ACCOUNT")
    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(credentials)


def open_sheet():
    print("Подключение к Google Sheets...")
    client = get_google_client()
    tasks_book = client.open_by_key(TASKS_SPREADSHEET_ID)
    tasks_sheet = tasks_book.worksheet(TASKS_SHEET_NAME)
    doctors_book = client.open_by_key(DOCTORS_SPREADSHEET_ID)
    doctors_sheet = doctors_book.worksheet(DOCTORS_SHEET_NAME)
    print("Google Sheets: OK")
    return tasks_sheet, doctors_sheet, tasks_book


# ============================================================
# LOG SHEET
# ============================================================

LOG_HEADERS = [
    "Timestamp", "Event", "BOTON-ID", "Дата", "Врач", "Email",
    "Mapping JSON", "Message-ID", "Result", "Notes",
]


def ensure_log_sheet(tasks_book):
    try:
        log_sheet = tasks_book.worksheet(LOG_SHEET_NAME)
        print(f"BoTon_Log: {LOG_SHEET_NAME}")
        return log_sheet
    except gspread.WorksheetNotFound:
        print("Google Sheets: создаю лист BoTon_Log")
        log_sheet = sheets_call(
            lambda: tasks_book.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_HEADERS)),
            "создание BoTon_Log",
        )
        sheets_call(
            lambda: log_sheet.append_row(LOG_HEADERS, value_input_option="USER_ENTERED"),
            "запись заголовков BoTon_Log",
        )
        return log_sheet


def make_log_row(event, bot_id="", target_date="", doctor="", email_address="", mapping=None, message_id="", result="", notes=""):
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
    for entry in entries:
        if isinstance(entry, dict):
            rows.append(make_log_row(
                event=entry.get("event", ""), bot_id=entry.get("bot_id", ""),
                target_date=entry.get("target_date", ""), doctor=entry.get("doctor", ""),
                email_address=entry.get("email_address", ""), mapping=entry.get("mapping", {}),
                message_id=entry.get("message_id", ""), result=entry.get("result", ""),
                notes=entry.get("notes", ""),
            ))
        else:
            rows.append(entry)
    sheets_call(lambda: log_sheet.append_rows(rows, value_input_option="USER_ENTERED"), "пакетная запись BoTon_Log")


def append_log(log_sheet, event, bot_id="", target_date="", doctor="", email_address="", mapping=None, message_id="", result="", notes=""):
    append_log_entries(log_sheet, [{
        "event": event, "bot_id": bot_id, "target_date": target_date, "doctor": doctor,
        "email_address": email_address, "mapping": mapping or {}, "message_id": message_id,
        "result": result, "notes": notes,
    }])


# ============================================================
# DOCTORS
# ============================================================

def load_doctors(doctors_sheet):
    values = doctors_sheet.get_all_values()
    doctors = []
    for row in values[1:]:
        if not row:
            continue
        name = row[0].strip() if len(row) > 0 else ""
        chat_id = row[1].strip() if len(row) > 1 else ""
        email_address = row[2].strip() if len(row) > 2 else ""
        if not name:
            continue
        doctors.append({"name": name, "chat_id": chat_id, "email": email_address})
    print(f"Врачей в базе: {len(doctors)}")
    return doctors


def extract_surname(value):
    value = normalize_text(value)
    parts = value.split()
    return parts[0] if parts else ""


def extract_initials(value):
    value = normalize_text(value)
    parts = value.split()
    return "".join([part[0] for part in parts[1:] if part])


def make_doctor_key(value):
    return re.sub(r"\s+", "", normalize_text(value))


def doctor_matches(doctor_name, task_doctor):
    a = normalize_text(doctor_name)
    b = normalize_text(task_doctor)
    if not a or not b:
        return False
    if a == b:
        return True
    surname_a = extract_surname(a)
    surname_b = extract_surname(b)
    if surname_a and surname_b and surname_a == surname_b:
        initials_a = extract_initials(a)
        initials_b = extract_initials(b)
        if not initials_a or not initials_b:
            return True
        if initials_a.startswith(initials_b) or initials_b.startswith(initials_a):
            return True
    if len(surname_a) >= 4 and surname_a in b:
        return True
    if len(surname_b) >= 4 and surname_b in a:
        return True
    return False


def find_doctor(task_doctor, doctors):
    for doctor in doctors:
        if doctor_matches(doctor["name"], task_doctor):
            return doctor
    return None


def is_branch_2(value):
    value = normalize_text(value)
    if not value:
        return False
    return any(keyword in value for keyword in BRANCH_KEYWORDS)


def parse_date(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    for fmt in ["%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"]:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def get_task_from_row(row, row_number):
    tasks = []
    columns = [(6, 7, 1), (8, 9, 2), (10, 11, 3)]
    for task_col, status_col, task_number in columns:
        task_value = row[task_col] if len(row) > task_col else ""
        if not str(task_value).strip():
            continue
        title = TASK_TITLES.get(task_number, f"Задание {task_number}")
        tasks.append({
            "task_col": task_col, "status_col": status_col, "task_number": task_number,
            "title": title, "text": str(task_value).strip(), "row_number": row_number,
        })
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
        if date_value != target_date:
            stats["other_date"] += 1
            continue
        if not is_branch_2(row[1]):
            stats["other_branch"] += 1
            continue
        
        if not FORCE_RESEND and not TEST_TO_RESERVE and str(row[12]).strip():
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

        rows.append({
            "row_number": row_number, "date": date_value, "branch": row[1],
            "doctor_name": doctor_name, "patient_id": row[3], "patient_name": row[4],
            "source": row[5], "tasks": tasks, "row": row,
        })
        stats["selected"] += 1
    return rows, stats


def group_by_doctor(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[normalize_text(row["doctor_name"])].append(row)
    return grouped


def build_patients(rows):
    return [{
        "patient_name": row["patient_name"], "patient_id": row["patient_id"],
        "row_number": row["row_number"], "tasks": row["tasks"],
    } for row in rows]


def make_bot_id(target_date, doctor, doctor_rows):
    parts = []
    for row in sorted(doctor_rows, key=lambda item: item["row_number"]):
        task_numbers = ",".join(str(task["task_number"]) for task in row["tasks"])
        parts.append(f"{row['row_number']}:{task_numbers}")
    
    signature = f"{target_date.strftime('%Y%m%d')}|{make_doctor_key(doctor)}|{'/'.join(parts)}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10].upper()
    return f"BOTON-{target_date.strftime('%Y%m%d')}-{make_doctor_key(doctor)}-{digest}"


def build_exact_response_task_map(patients):
    mapping = defaultdict(list)
    for patient_number, patient in enumerate(patients, start=1):
        tasks = patient["tasks"]
        for task_index, task in enumerate(tasks):
            key = f"{patient_number}" if len(tasks) == 1 else f"{patient_number}{TASK_LETTERS[task_index]}"
            mapping[key].append({
                "row_number": task["row_number"], "status_col": task["status_col"],
                "task_col": task["task_col"], "title": task["title"],
            })
    return dict(mapping)


def build_email_body(doctor, patients, bot_id):
    normalized = normalize_text(doctor["name"])
    parts = normalized.split()
    first_name = parts[1] if len(parts) >= 2 else ""
    patronymic = parts[2] if len(parts) >= 3 else ""
    
    if first_name and patronymic:
        address_name = f"{first_name.capitalize()} {patronymic.capitalize()}"
    elif first_name:
        address_name = first_name.capitalize()
    else:
        address_name = doctor["name"]

    lines = [
        f"Уважаемая(ый) {address_name}!", "",
        "Вам необходимо выполнить важные задания по записанным на сегодня к вам пациентам:", ""
    ]
    
    for patient_number, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        lines.append(f"{patient_number}. {p_name} — {p_id}")
        
        for task_index, task in enumerate(patient["tasks"]):
            prefix = "" if len(patient["tasks"]) == 1 else f"{TASK_LETTERS[task_index]}) "
            lines.append(f"   {prefix}{task['title']}")
            if task["text"]:
                lines.append(f"      {task['text']}")
        lines.append("")

    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    lines.extend([
        f"BOTON-ID: {bot_id}{resend_mark}",
        "Срок выполнения: сегодня.", "",
        "Прошу вернуть отработанное задание вашему заведующему.",
        "С уважением,", "BoTon и Агаввердиев Э.М.",
        now_moscow().strftime("%d.%m.%Y %H:%M:%S")
    ])
    return "\n".join(lines)


def send_email(recipient, subject, body):
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    username = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM_EMAIL", username)

    if not recipient:
        raise RuntimeError("У врача отсутствует email")

    message = MIMEMultipart()
    message["From"] = from_email
    message["To"] = recipient
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
        server.login(username, password)
        server.sendmail(from_email, [recipient], message.as_string())


def batch_update_cells(sheet, updates, description="пакетное обновление Google Sheets"):
    if not updates:
        return
    data = []
    for item in updates:
        start = gspread.utils.rowcol_to_a1(int(item["row"]), int(item["col"]))
        data.append({"range": f"{start}:{start}", "values": [[item.get("value", "")]]})
    sheets_call(lambda: sheet.batch_update(data, value_input_option="USER_ENTERED"), description)


def load_log_records(log_sheet):
    values = log_sheet.get_all_values()
    records = []
    for row in values[1:]:
        while len(row) < 10:
            row.append("")
        records.append({
            "timestamp": row[0], "event": row[1], "bot_id": row[2], "date": row[3],
            "doctor": row[4], "email": row[5], "mapping_json": row[6],
            "message_id": row[7], "result": row[8], "notes": row[9],
        })
    return records


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
    if not message_id:
        return False
    return any(record["event"] == "RECEIVE" and record["message_id"] == message_id for record in records)


def reconcile_sent_rows(tasks_sheet, records):
    sheet_values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение Лист1 для восстановления M")
    current_m = {row_number: str(row[12]).strip() if len(row) > 12 else "" for row_number, row in enumerate(sheet_values[1:], start=2)}
    
    updates = []
    seen_rows = set()
    for record in records:
        if record["event"] != "SEND" or record["result"] != "SENT" or not record["mapping_json"]:
            continue
        try:
            mapping = json.loads(record["mapping_json"])
        except Exception:
            continue
        
        bot_id = record["bot_id"]
        if not bot_id:
            continue
            
        resend_mark = " (ПОВТОРНО)" if "ПОВТОР" in record.get("notes", "") else ""
        value = f"Отправлено{resend_mark} {record['timestamp']} [{bot_id}]"
        
        for items in mapping.values():
            for item in items:
                row_number = int(item["row_number"])
                if row_number in seen_rows or current_m.get(row_number, ""):
                    continue
                seen_rows.add(row_number)
                updates.append({"row": row_number, "col": 13, "value": value})

    if updates:
        print(f"Восстановление M по BoTon_Log: {len(updates)} строк")
        batch_update_cells(tasks_sheet, updates, "восстановление M по SENT")


# ============================================================
# SEND ASSIGNMENTS
# ============================================================

def send_today_assignments(tasks_sheet, log_sheet, doctors_map, rows, target_date):
    grouped = group_by_doctor(rows)
    successful_doctors = 0
    successful_rows = 0
    total_tasks = 0
    reserve_count = 0
    errors = 0
    skipped_logged = 0

    records = load_log_records(log_sheet)
    
    if not TEST_TO_RESERVE:
        reconcile_sent_rows(tasks_sheet, records)

    prepared = []
    for doctor_key, doctor_rows in grouped.items():
        source_doctor_name = doctor_rows[0]["doctor_name"]
        doctor = find_doctor(source_doctor_name, doctors_map)

        if not doctor:
            print(f"Врач не найден: {source_doctor_name}")
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
        
        # В режиме TEST_TO_RESERVE все письма идут на REPORT_EMAIL
        if TEST_TO_RESERVE:
            email_address = REPORT_EMAIL
            is_reserve = True
        elif not email_address:
            email_address = REPORT_EMAIL
            reserve_count += 1
            is_reserve = True

        resend_mark = " (ПОВТОРНО)" if FORCE_RESEND else ""
        test_mark = " [ТЕСТ]" if TEST_TO_RESERVE else ""
        
        if TEST_TO_RESERVE:
            subject = f"[ТЕСТ] Для врача: {doctor['name']}{test_mark} [BOTON-ID:{bot_id}]"
        else:
            subject = f"Задания на сегодня{resend_mark} [BOTON-ID:{bot_id}]"
        
        body = build_email_body(doctor, patients, bot_id)

        prepared.append({
            "doctor": doctor, "doctor_rows": doctor_rows, "patients": patients,
            "mapping": mapping, "bot_id": bot_id, "email_address": email_address,
            "subject": subject, "body": body, "target_date": target_date.strftime("%d.%m.%Y"),
            "is_reserve": is_reserve,
        })

    # В режиме TEST_TO_RESERVE не записываем PENDING
    if not TEST_TO_RESERVE:
        pending_entries = []
        for item in prepared:
            notes = "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else ""
            pending_entries.append({
                "event": "SEND", "bot_id": item["bot_id"], "target_date": item["target_date"],
                "doctor": item["doctor"]["name"], "email_address": item["email_address"],
                "mapping": item["mapping"], "result": "PENDING", "notes": notes,
            })

        if pending_entries:
            print(f"BoTon_Log: пакетная запись PENDING ({len(pending_entries)} врачей)")
            append_log_entries(log_sheet, pending_entries)

    sent_log_entries = []
    error_log_entries = []
    m_updates = []

    for item in prepared:
        doctor = item["doctor"]
        doctor_rows = item["doctor_rows"]
        mapping = item["mapping"]
        bot_id = item["bot_id"]
        email_address = item["email_address"]

        try:
            send_email(email_address, item["subject"], item["body"])
            
            if TEST_TO_RESERVE:
                print(f"ТЕСТОВОЕ ПИСЬМО отправлено на {REPORT_EMAIL} (для врача: {doctor['name']})")
                successful_doctors += 1
                successful_rows += len(doctor_rows)
                total_tasks += sum(len(t["tasks"]) for t in doctor_rows)
            else:
                notes = "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else ""
                sent_log_entries.append({
                    "event": "SEND", "bot_id": bot_id, "target_date": item["target_date"],
                    "doctor": doctor["name"], "email_address": email_address,
                    "mapping": mapping, "result": "SENT", "notes": notes,
                })

                timestamp = now_moscow().strftime("%d.%m.%Y %H:%M:%S")
                resend_mark = " (ПОВТОРНО)" if FORCE_RESEND else ""
                m_value = f"Отправлено{resend_mark} {timestamp} [{bot_id}]"

                for row in doctor_rows:
                    m_updates.append({"row": row["row_number"], "col": 13, "value": m_value})

                successful_doctors += 1
                successful_rows += len(doctor_rows)
                total_tasks += sum(len(t["tasks"]) for t in doctor_rows)
                print(f"Письмо врачу отправлено: {doctor['name']} -> {email_address}")

        except Exception as exc:
            errors += 1
            if not TEST_TO_RESERVE:
                error_log_entries.append({
                    "event": "SEND", "bot_id": bot_id, "target_date": item["target_date"],
                    "doctor": doctor["name"], "email_address": email_address,
                    "mapping": mapping, "result": "ERROR", "notes": str(exc),
                })
            print(f"Ошибка отправки {doctor['name']}: {exc}")

    if not TEST_TO_RESERVE:
        final_log_entries = sent_log_entries + error_log_entries
        if final_log_entries:
            print(f"BoTon_Log: пакетная запись SENT/ERROR ({len(final_log_entries)})")
            append_log_entries(log_sheet, final_log_entries)

        if m_updates:
            print(f"Лист1: пакетная запись M ({len(m_updates)} строк)")
            batch_update_cells(tasks_sheet, m_updates, "пакетная запись M после отправки")

    print("ИТОГ РАССЫЛКИ:")
    print(f"Врачей обработано: {len(grouped)}")
    print(f"Успешно отправлено врачам: {successful_doctors}")
    if not TEST_TO_RESERVE:
        print(f"Пропущено по журналу: {skipped_logged}")
        print(f"Резерв: {reserve_count}")
    print(f"Ошибок: {errors}")
    print(f"Строк записано в M: {successful_rows}")
    print(f"Всего заданий: {total_tasks}")

    return {
        "doctors": successful_doctors,
        "rows": successful_rows,
        "tasks": total_tasks,
        "errors": errors,
        "skipped": skipped_logged,
        "reserve": reserve_count
    }


# ============================================================
# IMAP HELPERS
# ============================================================

def decode_mime_header(value):
    if not value:
        return ""
    parts = decode_header(value)
    result = []
    for part, encoding in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(encoding or "utf-8", errors="replace"))
            except Exception:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def extract_message_body(message):
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition.lower():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(charset, errors="replace"))
        return "\n".join(chunks)
    
    payload = message.get_payload(decode=True)
    if not payload:
        return ""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def search_doctor_messages(mail, doctor_emails):
    since_date = now_moscow().date() - timedelta(days=IMAP_LOOKBACK_DAYS)
    since_imap = since_date.strftime("%d-%b-%Y")
    print(f"IMAP: ищем письма врачей с {since_date.strftime('%d.%m.%Y')}")
    
    found = []
    seen = set()
    for email_address in doctor_emails:
        email_address = normalize_email(email_address)
        if not email_address:
            continue
        try:
            status, data = mail.search(None, "FROM", email_address, "SINCE", since_imap)
            if status != "OK" or not data or not data[0]:
                continue
            
            message_numbers = data[0].split()[-IMAP_MAX_PER_DOCTOR:]
            for num in message_numbers:
                key = num.decode(errors="ignore")
                if key not in seen:
                    seen.add(key)
                    found.append((num, email_address))
        except Exception as exc:
            print(f"IMAP: ошибка поиска {email_address}: {exc}")
            
    found = found[-IMAP_MAX_TOTAL:]
    print(f"IMAP: найдено потенциальных писем: {len(found)}")
    return found


def extract_bot_id(subject, body):
    text = f"{subject}\n{body}"
    match = re.search(r"BOTON-ID\s*[:\-]?\s*(BOTON-[A-Za-z0-9А-Яа-я_.-]+)", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def parse_status(text):
    normalized = normalize_text(text)
    if "не выполнено" in normalized:
        return "❌ Не выполнено"
    if "частично" in normalized:
        return "⚠️ Частично"
    if "выполнено" in normalized:
        return "✅ Выполнено"
    return None


def expand_status_items(text, mapping):
    result = []
    normalized = normalize_text(text)
    pattern = re.compile(r"(?:выполнено|частично|не\s+выполнено)\s*[:\-]?\s*([0-9]+[А-В]?)", flags=re.IGNORECASE)
    matches = pattern.findall(normalized)
    for key in matches:
        key = key.upper()
        if key in mapping:
            result.append(key)
    return result


STATUS_PRIORITY = {"": 0, "⏳ Нет ответа": 1, "✅ Выполнено": 2, "⚠️ Частично": 3, "❌ Не выполнено": 4}


def collect_status_update(updates, current_values, row_number, status_col, new_status):
    sheet_col = status_col + 1
    current = ""
    if row_number >= 1 and row_number - 1 < len(current_values):
        row = current_values[row_number - 1]
        if sheet_col - 1 < len(row):
            current = str(row[sheet_col - 1] or "").strip()

    old_priority = STATUS_PRIORITY.get(current, 0)
    new_priority = STATUS_PRIORITY.get(new_status, 0)
    if new_priority < old_priority:
        return

    key = (row_number, sheet_col)
    old_update = updates.get(key)
    if old_update:
        old_priority_2 = STATUS_PRIORITY.get(old_update, 0)
        if old_priority_2 >= new_priority:
            return
    updates[key] = new_status


def process_incoming_messages(tasks_sheet, log_sheet, doctors):
    print("\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----")
    host = os.environ.get("IMAP_HOST", "imap.yandex.ru")
    port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ.get("IMAP_USER", "")
    password = os.environ.get("IMAP_PASSWORD", "")

    doctor_emails = [normalize_email(doctor["email"]) for doctor in doctors if doctor["email"]]
    mail = None
    processed = 0

    try:
        print(f"IMAP: подключение к {host}:{port}...")
        mail = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT)
        mail.login(username, password)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            print("IMAP: не удалось открыть INBOX")
            return 0

        records = load_log_records(log_sheet)
        candidates = search_doctor_messages(mail, doctor_emails)
        current_values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение Лист1 перед обработкой ответов")

        status_updates = {}
        receive_logs = []
        receive_records = []

        for message_number, expected_email in candidates:
            try:
                status, data = mail.fetch(message_number, "(BODY.PEEK[])")
                if status != "OK" or not data:
                    continue

                raw_message = None
                for item in data:
                    if isinstance(item, tuple):
                        raw_message = item[1]
                        break
                if not raw_message:
                    continue

                message = email.message_from_bytes(raw_message)
                message_id = (message.get("Message-ID", "") or "").strip()

                if is_message_processed(records, message_id):
                    continue

                from_header = decode_mime_header(message.get("From", ""))
                subject = decode_mime_header(message.get("Subject", ""))
                body = extract_message_body(message)

                from_match = re.search(r"<([^>]+)>", from_header)
                sender_email = normalize_email(from_match.group(1) if from_match else from_header)

                if sender_email != expected_email:
                    continue

                doctor = next((item for item in doctors if normalize_email(item["email"]) == sender_email), None)
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

                text_for_status = f"{subject}\n{body}"
                status_value = parse_status(text_for_status)
                if not status_value:
                    continue

                keys = expand_status_items(text_for_status, mapping)
                if not keys:
                    continue

                updated = []
                for key in keys:
                    for item in mapping.get(key, []):
                        collect_status_update(status_updates, current_values, item["row_number"], item["status_col"], status_value)
                        updated.append({
                            "key": key, "row": item["row_number"], "status_col": item["status_col"],
                            "title": item["title"], "status": status_value,
                        })

                receive_logs.append({
                    "event": "RECEIVE", "bot_id": bot_id, "target_date": log_record["date"],
                    "doctor": doctor["name"], "email_address": sender_email, "mapping": mapping,
                    "message_id": message_id, "result": "PROCESSED",
                    "notes": json.dumps(updated, ensure_ascii=False),
                })
                receive_records.append({
                    "timestamp": now_moscow().strftime("%d.%m.%Y %H:%M:%S"), "event": "RECEIVE",
                    "bot_id": bot_id, "date": log_record["date"], "doctor": doctor["name"],
                    "email": sender_email, "mapping_json": json.dumps(mapping, ensure_ascii=False),
                    "message_id": message_id, "result": "PROCESSED",
                    "notes": json.dumps(updated, ensure_ascii=False),
                })
                processed += 1
                print(f"Ответ подготовлен: {doctor['name']} [{bot_id}]")

            except Exception as exc:
                print(f"IMAP: ошибка обработки сообщения: {exc}")

        if status_updates:
            updates = [{"row": rn, "col": sc, "value": val} for (rn, sc), val in status_updates.items()]
            print(f"Лист1: пакетная запись статусов ({len(updates)} ячеек)")
            batch_update_cells(tasks_sheet, updates, "пакетная запись H/J/L")

        if receive_logs:
            print(f"BoTon_Log: пакетная запись RECEIVE ({len(receive_logs)})")
            append_log_entries(log_sheet, receive_logs)
            records.extend(receive_records)

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
# REPORT TO HEAD OF DEPARTMENT (PLAIN TEXT)
# ============================================================

def send_summary_report(target_date, send_stats, received_count):
    print("\n----- ФОРМИРОВАНИЕ И ОТПРАВКА ОТЧЕТА -----")
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    test_mark = " (ТЕСТОВЫЙ ПРОГОН НА РЕЗЕРВ)" if TEST_TO_RESERVE else ""
    
    text_body = (
        f"ОТЧЕТ СИСТЕМЫ BoTon v{VERSION}{resend_mark}{test_mark}\n"
        f"Дата формирования: {target_date.strftime('%d.%m.%Y %H:%M')}\n"
        f"{'='*45}\n\n"
        f"СТАТИСТИКА РАССЫЛКИ:\n"
        f"  - Врачей обработано: ........... {send_stats['doctors']}\n"
        f"  - Успешно отправлено: .......... {send_stats['doctors'] - send_stats['errors']}\n"
    )
    
    if TEST_TO_RESERVE:
        text_body += f"  - Тестовых отправлений на резерв: {send_stats['doctors']}\n"
    else:
        text_body += f"  - Отправлено на резерв (нет email): {send_stats['reserve']}\n"
    
    text_body += (
        f"  - Ошибок отправки: ............. {send_stats['errors']}\n"
        f"  - Строк обновлено в таблице (M): {send_stats['rows']}\n"
        f"  - Всего заданий в рассылке: .... {send_stats['tasks']}\n\n"
        f"ВХОДЯЩИЕ ОТВЕТЫ:\n"
        f"  - Получено и обработано ответов: {received_count}\n\n"
        f"{'='*45}\n"
        f"Система работает автоматически.\n"
        f"При возникновении вопросов обращайтесь к администратору."
    )
    
    subject = f"[ОТЧЕТ BoTon] Рассылка и ответы за {target_date.strftime('%d.%m.%Y')}{resend_mark}{test_mark}"
    
    try:
        send_email(REPORT_EMAIL, subject, text_body)
        print(f"ОТЧЕТ УСПЕШНО ОТПРАВЛЕН на {REPORT_EMAIL}")
    except Exception as exc:
        print(f"ОШИБКА ОТПРАВКИ ОТЧЕТА: {exc}")


# ============================================================
# MAIN
# ============================================================

def main():
    print(f"BOTON v{VERSION}")
    print(f"TEST_MODE = {TEST_MODE}")
    print(f"TEST_RECEIVE_ONLY = {TEST_RECEIVE_ONLY}")
    print(f"TEST_TO_RESERVE = {TEST_TO_RESERVE}")
    print(f"FORCE_RESEND = {FORCE_RESEND}")

    target_date = now_moscow().date()
    print(f"Дата: {target_date.strftime('%d.%m.%Y')}")
    print(f"Текущее время МСК: {now_moscow().strftime('%d.%m.%Y %H:%M:%S')}")

    tasks_sheet, doctors_sheet, tasks_book = open_sheet()
    log_sheet = ensure_log_sheet(tasks_book)
    doctors = load_doctors(doctors_sheet)

    rows, stats = load_task_rows(tasks_sheet, target_date)
    print(f"Подошло для обработки: {len(rows)} строк")

    received = process_incoming_messages(tasks_sheet, log_sheet, doctors)
    print(f"Обработано входящих ответов: {received}")

    send_stats = {"doctors": 0, "rows": 0, "tasks": 0, "errors": 0, "skipped": 0, "reserve": 0}
    
    if not TEST_RECEIVE_ONLY:
        if is_assignment_time() or FORCE_RESEND or TEST_TO_RESERVE:
            if TEST_TO_RESERVE:
                print("\n----- ТЕСТОВЫЙ ПРОГОН: ОТПРАВКА НА РЕЗЕРВ -----")
                print("Все письма врачей будут отправлены на", REPORT_EMAIL)
            elif FORCE_RESEND:
                print("\n----- ПРИНУДИТЕЛЬНАЯ РАССЫЛКА (FORCE_RESEND) -----")
            else:
                print("\n----- РАССЫЛКА -----")
                print("Рассылка разрешена: 08:00–12:00 МСК")
            
            send_stats = send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date)
        else:
            print("\n----- РАССЫЛКА НЕ ВЫПОЛНЯЕТСЯ -----")
            print("Рассылка заданий разрешена только с 08:00 до 12:00 МСК.")

    send_summary_report(target_date, send_stats, received)

    print(f"\nBOTON v{VERSION} завершён успешно.")


if __name__ == "__main__":
    main()
