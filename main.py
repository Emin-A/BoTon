# ============================================================
# BoTon v2.3
# Рассылка заданий + приём ответов врачей + запись H/J/L
# + точная привязка ответа к отправленному заданию
# + защищённый IMAP без STORE/UNSEEN
# + журнал BoTon_Log
# + рассылка заданий только 08:00–12:00 МСК
#
# VERSION: 2.3
# ============================================================

import os
import re
import json
import ssl
import smtplib
import secrets
import imaplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIG
# ============================================================

VERSION = "2.3"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

TASKS_SPREADSHEET_ID = (
    "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
)

TASKS_SHEET_NAME = "Лист1"

DOCTORS_SPREADSHEET_ID = (
    "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
)

DOCTORS_SHEET_NAME = "Врачи"

LOG_SHEET_NAME = "BoTon_Log"


# ============================================================
# IMAP
# ============================================================

IMAP_TIMEOUT = 20

IMAP_LOOKBACK_DAYS = 3

IMAP_MAX_PER_DOCTOR = 20

IMAP_MAX_TOTAL = 100


# ============================================================
# TEST MODE
# ============================================================

TEST_MODE = False

TEST_RECEIVE_ONLY = False


# ============================================================
# BRANCH
# ============================================================

BRANCH_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]


# ============================================================
# TASK LETTERS
# ============================================================

TASK_LETTERS = [
    "А",
    "Б",
    "В",
]


# ============================================================
# TASK TITLES
# ============================================================

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
    """
    Рассылка заданий разрешена только
    с 08:00 до 12:00 по московскому времени.

    Входящие ответы принимаются круглосуточно.
    """

    current_time = now_moscow().time()

    return time(8, 0) <= current_time <= time(12, 0)


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_text(value):
    if value is None:
        return ""

    value = str(value)

    value = value.replace("Ё", "Е")
    value = value.replace("ё", "е")

    value = value.replace("\xa0", " ")

    value = re.sub(
        r"[^\w\sА-Яа-я]",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip().lower()


def normalize_email(value):
    return str(value or "").strip().lower()


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_google_client():

    raw = os.environ.get(
        "GOOGLE_SERVICE_ACCOUNT",
        "",
    )

    if not raw:
        raise RuntimeError(
            "Не найден GOOGLE_SERVICE_ACCOUNT"
        )

    info = json.loads(raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        info,
        scopes=scopes,
    )

    return gspread.authorize(credentials)


def open_sheet():

    print(
        "Подключение к Google Sheets..."
    )

    client = get_google_client()

    tasks_book = client.open_by_key(
        TASKS_SPREADSHEET_ID
    )

    tasks_sheet = tasks_book.worksheet(
        TASKS_SHEET_NAME
    )

    doctors_book = client.open_by_key(
        DOCTORS_SPREADSHEET_ID
    )

    doctors_sheet = doctors_book.worksheet(
        DOCTORS_SHEET_NAME
    )

    print(
        "Google Sheets: OK"
    )

    return (
        tasks_sheet,
        doctors_sheet,
        tasks_book,
    )


# ============================================================
# LOG SHEET
# ============================================================

LOG_HEADERS = [
    "Timestamp",
    "Event",
    "BOTON-ID",
    "Дата",
    "Врач",
    "Email",
    "Mapping JSON",
    "Message-ID",
    "Result",
    "Notes",
]


def ensure_log_sheet(tasks_book):

    try:

        log_sheet = tasks_book.worksheet(
            LOG_SHEET_NAME
        )

        print(
            f"BoTon_Log: {LOG_SHEET_NAME}"
        )

        return log_sheet

    except gspread.WorksheetNotFound:

        print(
            "Google Sheets: создаю лист BoTon_Log"
        )

        log_sheet = tasks_book.add_worksheet(
            title=LOG_SHEET_NAME,
            rows=1000,
            cols=len(LOG_HEADERS),
        )

        log_sheet.append_row(
            LOG_HEADERS
        )

        return log_sheet


def append_log(
    log_sheet,
    event,
    bot_id="",
    target_date="",
    doctor="",
    email_address="",
    mapping=None,
    message_id="",
    result="",
    notes="",
):

    row = [
        now_moscow().strftime(
            "%d.%m.%Y %H:%M:%S"
        ),
        event,
        bot_id,
        target_date,
        doctor,
        email_address,
        json.dumps(
            mapping or {},
            ensure_ascii=False,
        ),
        message_id,
        result,
        notes,
    ]

    log_sheet.append_row(
        row,
        value_input_option="USER_ENTERED",
    )


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

        email_address = (
            row[2].strip()
            if len(row) > 2
            else ""
        )

        if not name:
            continue

        doctors.append(
            {
                "name": name,
                "chat_id": chat_id,
                "email": email_address,
            }
        )

    print(
        f"Врачей в базе: {len(doctors)}"
    )

    return doctors


# ============================================================
# DOCTOR NAME HELPERS
# ============================================================

def extract_surname(value):

    value = normalize_text(value)

    parts = value.split()

    if not parts:
        return ""

    return parts[0]


def extract_initials(value):

    value = normalize_text(value)

    parts = value.split()

    initials = []

    for part in parts[1:]:

        if part:
            initials.append(
                part[0]
            )

    return "".join(initials)


def make_doctor_key(value):

    value = normalize_text(value)

    value = re.sub(
        r"\s+",
        "",
        value,
    )

    return value


def doctor_matches(
    doctor_name,
    task_doctor,
):

    a = normalize_text(
        doctor_name
    )

    b = normalize_text(
        task_doctor
    )

    if not a or not b:
        return False

    if a == b:
        return True

    surname_a = extract_surname(a)
    surname_b = extract_surname(b)

    if (
        surname_a
        and surname_b
        and surname_a == surname_b
    ):

        initials_a = extract_initials(a)
        initials_b = extract_initials(b)

        if (
            not initials_a
            or not initials_b
        ):
            return True

        if (
            initials_a.startswith(initials_b)
            or initials_b.startswith(initials_a)
        ):
            return True

    if (
        len(surname_a) >= 4
        and surname_a in b
    ):
        return True

    if (
        len(surname_b) >= 4
        and surname_b in a
    ):
        return True

    return False


def find_doctor(
    task_doctor,
    doctors,
):

    for doctor in doctors:

        if doctor_matches(
            doctor["name"],
            task_doctor,
        ):

            return doctor

    return None


# ============================================================
# BRANCH
# ============================================================

def is_branch_2(value):

    value = normalize_text(value)

    if not value:
        return False

    for keyword in BRANCH_KEYWORDS:

        if keyword in value:
            return True

    return False


# ============================================================
# DATE
# ============================================================

def parse_date(value):

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d",
        "%d/%m/%Y",
    ]

    for fmt in formats:

        try:

            return datetime.strptime(
                value,
                fmt,
            ).date()

        except ValueError:
            pass

    return None


# ============================================================
# TASK EXTRACTION
# ============================================================

def get_task_from_row(
    row,
    row_number,
):

    tasks = []

    columns = [
        (
            6,
            7,
            1,
        ),
        (
            8,
            9,
            2,
        ),
        (
            10,
            11,
            3,
        ),
    ]

    for task_col, status_col, task_number in columns:

        task_value = (
            row[task_col]
            if len(row) > task_col
            else ""
        )

        if not str(task_value).strip():
            continue

        title = TASK_TITLES.get(
            task_number,
            f"Задание {task_number}",
        )

        tasks.append(
            {
                "task_col": task_col,
                "status_col": status_col,
                "task_number": task_number,
                "title": title,
                "text": str(task_value).strip(),
                "row_number": row_number,
            }
        )

    return tasks


# ============================================================
# LOAD TASKS
# ============================================================

def load_task_rows(
    tasks_sheet,
    target_date,
):

    values = tasks_sheet.get_all_values()

    rows = []

    stats = {
        "total": 0,
        "empty": 0,
        "bad_date": 0,
        "other_date": 0,
        "other_branch": 0,
        "already_processed": 0,
        "no_doctor": 0,
        "doctor_not_found": 0,
        "no_tasks": 0,
        "selected": 0,
    }

    for row_number, row in enumerate(
        values[1:],
        start=2,
    ):

        stats["total"] += 1

        if not row or not any(
            str(x).strip()
            for x in row
        ):

            stats["empty"] += 1

            continue

        row = list(row)

        while len(row) < 13:
            row.append("")

        date_value = parse_date(
            row[0]
        )

        if date_value is None:

            stats["bad_date"] += 1

            continue

        if date_value != target_date:

            stats["other_date"] += 1

            continue

        if not is_branch_2(row[1]):

            stats["other_branch"] += 1

            continue

        if str(row[12]).strip():

            stats["already_processed"] += 1

            continue

        doctor_name = str(
            row[2]
        ).strip()

        if not doctor_name:

            stats["no_doctor"] += 1

            continue

        tasks = get_task_from_row(
            row,
            row_number,
        )

        if not tasks:

            stats["no_tasks"] += 1

            continue

        rows.append(
            {
                "row_number": row_number,
                "date": date_value,
                "branch": row[1],
                "doctor_name": doctor_name,
                "patient_id": row[3],
                "patient_name": row[4],
                "source": row[5],
                "tasks": tasks,
                "row": row,
            }
        )

        stats["selected"] += 1

    return rows, stats


# ============================================================
# GROUP PATIENTS
# ============================================================

def group_by_doctor(rows):

    grouped = defaultdict(list)

    for row in rows:

        grouped[
            normalize_text(
                row["doctor_name"]
            )
        ].append(row)

    return grouped


def build_patients(rows):

    patients = []

    for row in rows:

        patients.append(
            {
                "patient_name": row[
                    "patient_name"
                ],
                "patient_id": row[
                    "patient_id"
                ],
                "row_number": row[
                    "row_number"
                ],
                "tasks": row[
                    "tasks"
                ],
            }
        )

    return patients


# ============================================================
# BOTON ID
# ============================================================

def make_bot_id(
    target_date,
    doctor,
):

    token = secrets.token_hex(
        2
    ).upper()

    return (
        f"BOTON-"
        f"{target_date.strftime('%Y%m%d')}-"
        f"{make_doctor_key(doctor)}-"
        f"{token}"
    )


# ============================================================
# EXACT RESPONSE MAP
# ============================================================

def build_exact_response_task_map(
    patients,
):

    mapping = defaultdict(list)

    for patient_number, patient in enumerate(
        patients,
        start=1,
    ):

        tasks = patient["tasks"]

        for task_index, task in enumerate(
            tasks
        ):

            if len(tasks) == 1:

                key = f"{patient_number}"

            else:

                key = (
                    f"{patient_number}"
                    f"{TASK_LETTERS[task_index]}"
                )

            mapping[key].append(
                {
                    "row_number": task[
                        "row_number"
                    ],
                    "status_col": task[
                        "status_col"
                    ],
                    "task_col": task[
                        "task_col"
                    ],
                    "title": task[
                        "title"
                    ],
                }
            )

    return dict(mapping)


# ============================================================
# EMAIL BODY
# ============================================================

def build_email_body(
    doctor,
    patients,
    bot_id,
):

    first_name = ""
    patronymic = ""

    normalized = normalize_text(
        doctor["name"]
    )

    parts = normalized.split()

    if len(parts) >= 3:

        first_name = parts[1]
        patronymic = parts[2]

    elif len(parts) == 2:

        first_name = parts[1]

    if first_name and patronymic:

        address_name = (
            f"{first_name.capitalize()} "
            f"{patronymic.capitalize()}"
        )

    elif first_name:

        address_name = (
            first_name.capitalize()
        )

    else:

        address_name = doctor["name"]

    lines = []

    lines.append(
        f"Уважаемая(ый) {address_name}!"
    )

    lines.append("")

    lines.append(
        "Вам необходимо выполнить важные "
        "задания по записанным на сегодня "
        "к вам пациентам:"
    )

    lines.append("")

    for patient_number, patient in enumerate(
        patients,
        start=1,
    ):

        patient_name = (
            patient["patient_name"]
            or "ФИО не указано"
        )

        patient_id = (
            patient["patient_id"]
            or "не указан"
        )

        tasks = patient["tasks"]

        lines.append(
            f"{patient_number}. "
            f"{patient_name} — "
            f"{patient_id}"
        )

        for task_index, task in enumerate(
            tasks
        ):

            if len(tasks) == 1:

                prefix = ""

            else:

                prefix = (
                    f"{TASK_LETTERS[task_index]}) "
                )

            lines.append(
                f"   {prefix}{task['title']}"
            )

            if task["text"]:

                lines.append(
                    f"      {task['text']}"
                )

        lines.append("")

    lines.append(
        f"BOTON-ID: {bot_id}"
    )

    lines.append(
        "Срок выполнения: сегодня."
    )

    lines.append("")

    lines.append(
        "Прошу вернуть отработанное задание "
        "вашему заведующему."
    )

    lines.append(
        "С уважением,"
    )

    lines.append(
        "BoTon и Агаввердиев Э.М."
    )

    lines.append(
        now_moscow().strftime(
            "%d.%m.%Y %H:%M:%S"
        )
    )

    return "\n".join(lines)


# ============================================================
# SMTP
# ============================================================

def send_email(
    recipient,
    subject,
    body,
):

    host = os.environ.get(
        "SMTP_HOST",
        "",
    )

    port = int(
        os.environ.get(
            "SMTP_PORT",
            "465",
        )
    )

    username = os.environ.get(
        "SMTP_USER",
        "",
    )

    password = os.environ.get(
        "SMTP_PASSWORD",
        "",
    )

    from_email = os.environ.get(
        "SMTP_FROM_EMAIL",
        username,
    )

    if not recipient:

        raise RuntimeError(
            "У врача отсутствует email"
        )

    message = MIMEMultipart()

    message["From"] = from_email

    message["To"] = recipient

    message["Subject"] = subject

    message.attach(
        MIMEText(
            body,
            "plain",
            "utf-8",
        )
    )

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        host,
        port,
        context=context,
        timeout=30,
    ) as server:

        server.login(
            username,
            password,
        )

        server.sendmail(
            from_email,
            [recipient],
            message.as_string(),
        )


# ============================================================
# UPDATE COLUMN M
# ============================================================

def mark_rows_sent(
    tasks_sheet,
    row_numbers,
    bot_id,
):

    timestamp = now_moscow().strftime(
        "%d.%m.%Y %H:%M:%S"
    )

    value = (
        f"Отправлено {timestamp} "
        f"[{bot_id}]"
    )

    for row_number in row_numbers:

        tasks_sheet.update_cell(
            row_number,
            13,
            value,
        )


# ============================================================
# SEND ASSIGNMENTS
# ============================================================

def send_today_assignments(
    tasks_sheet,
    log_sheet,
    doctors_map,
    rows,
    target_date,
):

    grouped = group_by_doctor(
        rows
    )

    successful_doctors = 0

    successful_rows = 0

    total_tasks = 0

    reserve_count = 0

    errors = 0

    for doctor_key, doctor_rows in grouped.items():

        source_doctor_name = doctor_rows[0][
            "doctor_name"
        ]

        doctor = find_doctor(
            source_doctor_name,
            doctors_map,
        )

        if not doctor:

            print(
                f"Врач не найден: "
                f"{source_doctor_name}"
            )

            errors += 1

            continue

        patients = build_patients(
            doctor_rows
        )

        mapping = build_exact_response_task_map(
            patients
        )

        bot_id = make_bot_id(
            target_date,
            doctor["name"],
        )

        email_address = (
            normalize_email(
                doctor["email"]
            )
        )

        if not email_address:

            email_address = (
                "eagaverdiev@mosmedzdrav.ru"
            )

            reserve_count += 1

        subject = (
            f"Задания на сегодня "
            f"[BOTON-ID:{bot_id}]"
        )

        body = build_email_body(
            doctor,
            patients,
            bot_id,
        )

        append_log(
            log_sheet,
            event="SEND",
            bot_id=bot_id,
            target_date=target_date.strftime(
                "%d.%m.%Y"
            ),
            doctor=doctor["name"],
            email_address=email_address,
            mapping=mapping,
            result="PENDING",
        )

        try:

            send_email(
                email_address,
                subject,
                body,
            )

            row_numbers = [
                item["row_number"]
                for item in doctor_rows
            ]

            mark_rows_sent(
                tasks_sheet,
                row_numbers,
                bot_id,
            )

            successful_doctors += 1

            successful_rows += len(
                doctor_rows
            )

            total_tasks += sum(
                len(
                    item["tasks"]
                )
                for item in doctor_rows
            )

            append_log(
                log_sheet,
                event="SEND",
                bot_id=bot_id,
                target_date=target_date.strftime(
                    "%d.%m.%Y"
                ),
                doctor=doctor["name"],
                email_address=email_address,
                mapping=mapping,
                result="SENT",
            )

            print(
                f"Письмо врачу отправлено "
                f"{doctor['name']} -> "
                f"{email_address}"
            )

        except Exception as exc:

            errors += 1

            append_log(
                log_sheet,
                event="SEND",
                bot_id=bot_id,
                target_date=target_date.strftime(
                    "%d.%m.%Y"
                ),
                doctor=doctor["name"],
                email_address=email_address,
                mapping=mapping,
                result="ERROR",
                notes=str(exc),
            )

            print(
                f"Ошибка отправки "
                f"{doctor['name']}: {exc}"
            )

    print("ИТОГ:")

    print(
        f"Врачей обработано: "
        f"{len(grouped)}"
    )

    print(
        f"Успешно отправлено врачам: "
        f"{successful_doctors}"
    )

    print(
        f"Резерв: {reserve_count}"
    )

    print(
        f"Ошибок: {errors}"
    )

    print(
        f"Строк записано в M: "
        f"{successful_rows}"
    )

    print(
        f"Всего заданий: "
        f"{total_tasks}"
    )


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

                result.append(
                    part.decode(
                        encoding or "utf-8",
                        errors="replace",
                    )
                )

            except Exception:

                result.append(
                    part.decode(
                        "utf-8",
                        errors="replace",
                    )
                )

        else:

            result.append(str(part))

    return "".join(result)


def extract_message_body(
    message,
):

    if message.is_multipart():

        chunks = []

        for part in message.walk():

            content_type = part.get_content_type()

            disposition = str(
                part.get(
                    "Content-Disposition",
                    "",
                )
            )

            if (
                content_type == "text/plain"
                and "attachment" not in disposition.lower()
            ):

                payload = part.get_payload(
                    decode=True
                )

                if payload:

                    charset = (
                        part.get_content_charset()
                        or "utf-8"
                    )

                    chunks.append(
                        payload.decode(
                            charset,
                            errors="replace",
                        )
                    )

        return "\n".join(chunks)

    payload = message.get_payload(
        decode=True
    )

    if not payload:
        return ""

    charset = (
        message.get_content_charset()
        or "utf-8"
    )

    return payload.decode(
        charset,
        errors="replace",
    )


# ============================================================
# IMAP SEARCH
# ============================================================

def search_doctor_messages(
    mail,
    doctor_emails,
):

    since_date = (
        now_moscow().date()
        - timedelta(
            days=IMAP_LOOKBACK_DAYS
        )
    )

    since_imap = since_date.strftime(
        "%d-%b-%Y"
    )

    print(
        "IMAP: ищем письма врачей "
        f"с {since_date.strftime('%d.%m.%Y')}"
    )

    found = []

    seen = set()

    for email_address in doctor_emails:

        email_address = normalize_email(
            email_address
        )

        if not email_address:
            continue

        try:

            status, data = mail.search(
                None,
                "FROM",
                email_address,
                "SINCE",
                since_imap,
            )

            if status != "OK":
                continue

            if not data or not data[0]:
                continue

            message_numbers = (
                data[0]
                .split()
            )

            for num in message_numbers:

                key = num.decode(
                    errors="ignore"
                )

                if key in seen:
                    continue

                seen.add(key)

                found.append(
                    (
                        num,
                        email_address,
                    )
                )

        except Exception as exc:

            print(
                f"IMAP: ошибка поиска "
                f"{email_address}: {exc}"
            )

    found = found[
        -IMAP_MAX_TOTAL:
    ]

    print(
        "IMAP: найдено потенциальных "
        f"писем: {len(found)}"
    )

    return found


# ============================================================
# LOG LOOKUP
# ============================================================

def load_log_records(
    log_sheet,
):

    values = log_sheet.get_all_values()

    records = []

    for row in values[1:]:

        while len(row) < 10:
            row.append("")

        records.append(
            {
                "timestamp": row[0],
                "event": row[1],
                "bot_id": row[2],
                "date": row[3],
                "doctor": row[4],
                "email": row[5],
                "mapping_json": row[6],
                "message_id": row[7],
                "result": row[8],
                "notes": row[9],
            }
        )

    return records


def find_log_by_bot_id(
    records,
    bot_id,
):

    for record in reversed(records):

        if (
            record["bot_id"]
            == bot_id
            and record["event"]
            == "SEND"
            and record["result"]
            == "SENT"
        ):

            return record

    return None


def is_message_processed(
    records,
    message_id,
):

    if not message_id:
        return False

    for record in records:

        if (
            record["event"]
            == "RECEIVE"
            and record["message_id"]
            == message_id
        ):

            return True

    return False


# ============================================================
# BOTON ID EXTRACTION
# ============================================================

def extract_bot_id(
    subject,
    body,
):

    text = (
        f"{subject}\n{body}"
    )

    match = re.search(
        r"BOTON-ID\s*[:\-]?\s*"
        r"(BOTON-[A-Za-z0-9А-Яа-я_.-]+)",
        text,
        flags=re.IGNORECASE,
    )

    if match:

        return match.group(1)

    return ""


# ============================================================
# STATUS
# ============================================================

def parse_status(
    text,
):

    normalized = normalize_text(
        text
    )

    if (
        "не выполнено"
        in normalized
    ):

        return "❌ Не выполнено"

    if (
        "частично"
        in normalized
    ):

        return "⚠️ Частично"

    if (
        "выполнено"
        in normalized
    ):

        return "✅ Выполнено"

    return None


# ============================================================
# STATUS ITEMS
# ============================================================

def expand_status_items(
    text,
    mapping,
):

    result = []

    normalized = normalize_text(
        text
    )

    # Выполнено: 2
    # Выполнено: 2А
    pattern = re.compile(
        r"(?:выполнено|частично|"
        r"не\s+выполнено)"
        r"\s*[:\-]?\s*"
        r"([0-9]+[А-В]?)",
        flags=re.IGNORECASE,
    )

    matches = pattern.findall(
        normalized
    )

    for key in matches:

        key = key.upper()

        if key in mapping:

            result.append(
                key
            )

    return result


# ============================================================
# UPDATE STATUS
# ============================================================

STATUS_PRIORITY = {
    "": 0,
    "⏳ Нет ответа": 1,
    "✅ Выполнено": 2,
    "⚠️ Частично": 3,
    "❌ Не выполнено": 4,
}


def update_status_cell(
    tasks_sheet,
    row_number,
    status_col,
    new_status,
):

    current = tasks_sheet.cell(
        row_number,
        status_col + 1,
    ).value or ""

    current = str(current).strip()

    if (
        STATUS_PRIORITY.get(
            new_status,
            0,
        )
        >= STATUS_PRIORITY.get(
            current,
            0,
        )
    ):

        tasks_sheet.update_cell(
            row_number,
            status_col + 1,
            new_status,
        )


# ============================================================
# PROCESS INCOMING MAIL
# ============================================================

def process_incoming_messages(
    tasks_sheet,
    log_sheet,
    doctors,
):

    print(
        "\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----"
    )

    host = os.environ.get(
        "IMAP_HOST",
        "imap.yandex.ru",
    )

    port = int(
        os.environ.get(
            "IMAP_PORT",
            "993",
        )
    )

    username = os.environ.get(
        "IMAP_USER",
        "",
    )

    password = os.environ.get(
        "IMAP_PASSWORD",
        "",
    )

    doctor_emails = [
        normalize_email(
            doctor["email"]
        )
        for doctor in doctors
        if doctor["email"]
    ]

    mail = None

    processed = 0

    try:

        print(
            f"IMAP: подключение к "
            f"{host}:{port}..."
        )

        mail = imaplib.IMAP4_SSL(
            host,
            port,
            timeout=IMAP_TIMEOUT,
        )

        print(
            "IMAP: SSL-соединение установлено"
        )

        print(
            "IMAP: авторизация пользователя "
            f"{username}..."
        )

        mail.login(
            username,
            password,
        )

        print(
            "IMAP: авторизация OK"
        )

        print(
            "IMAP: открытие INBOX..."
        )

        status, _ = mail.select(
            "INBOX",
            readonly=True,
        )

        if status != "OK":

            print(
                "IMAP: не удалось открыть INBOX"
            )

            return 0

        print(
            "IMAP: INBOX открыт "
            "в режиме readonly"
        )

        records = load_log_records(
            log_sheet
        )

        candidates = search_doctor_messages(
            mail,
            doctor_emails,
        )

        for message_number, expected_email in candidates:

            try:

                status, data = mail.fetch(
                    message_number,
                    "(BODY.PEEK[])",
                )

                if status != "OK":
                    continue

                if not data:
                    continue

                raw_message = None

                for item in data:

                    if isinstance(
                        item,
                        tuple,
                    ):

                        raw_message = item[1]

                        break

                if not raw_message:
                    continue

                message = email.message_from_bytes(
                    raw_message
                )

                message_id = (
                    message.get(
                        "Message-ID",
                        "",
                    )
                    or ""
                ).strip()

                if is_message_processed(
                    records,
                    message_id,
                ):

                    continue

                from_header = decode_mime_header(
                    message.get(
                        "From",
                        "",
                    )
                )

                subject = decode_mime_header(
                    message.get(
                        "Subject",
                        "",
                    )
                )

                body = extract_message_body(
                    message
                )

                from_match = re.search(
                    r"<([^>]+)>",
                    from_header,
                )

                sender_email = (
                    from_match.group(1)
                    if from_match
                    else from_header
                )

                sender_email = normalize_email(
                    sender_email
                )

                if (
                    sender_email
                    != expected_email
                ):

                    continue

                doctor = None

                for item in doctors:

                    if (
                        normalize_email(
                            item["email"]
                        )
                        == sender_email
                    ):

                        doctor = item

                        break

                if not doctor:
                    continue

                bot_id = extract_bot_id(
                    subject,
                    body,
                )

                if not bot_id:
                    continue

                log_record = find_log_by_bot_id(
                    records,
                    bot_id,
                )

                if not log_record:
                    continue

                try:

                    mapping = json.loads(
                        log_record[
                            "mapping_json"
                        ]
                        or "{}"
                    )

                except Exception:

                    mapping = {}

                if not mapping:
                    continue

                text_for_status = (
                    f"{subject}\n{body}"
                )

                status_value = parse_status(
                    text_for_status
                )

                if not status_value:
                    continue

                keys = expand_status_items(
                    text_for_status,
                    mapping,
                )

                if not keys:
                    continue

                updated = []

                for key in keys:

                    for item in mapping.get(
                        key,
                        [],
                    ):

                        update_status_cell(
                            tasks_sheet,
                            item[
                                "row_number"
                            ],
                            item[
                                "status_col"
                            ],
                            status_value,
                        )

                        updated.append(
                            {
                                "key": key,
                                "row": item[
                                    "row_number"
                                ],
                                "status_col": item[
                                    "status_col"
                                ],
                                "title": item[
                                    "title"
                                ],
                                "status": status_value,
                            }
                        )

                append_log(
                    log_sheet,
                    event="RECEIVE",
                    bot_id=bot_id,
                    target_date=log_record[
                        "date"
                    ],
                    doctor=doctor["name"],
                    email_address=sender_email,
                    mapping=mapping,
                    message_id=message_id,
                    result="PROCESSED",
                    notes=json.dumps(
                        updated,
                        ensure_ascii=False,
                    ),
                )

                records.append(
                    {
                        "timestamp":
                            now_moscow().strftime(
                                "%d.%m.%Y %H:%M:%S"
                            ),
                        "event":
                            "RECEIVE",
                        "bot_id":
                            bot_id,
                        "date":
                            log_record["date"],
                        "doctor":
                            doctor["name"],
                        "email":
                            sender_email,
                        "mapping_json":
                            json.dumps(
                                mapping,
                                ensure_ascii=False,
                            ),
                        "message_id":
                            message_id,
                        "result":
                            "PROCESSED",
                        "notes":
                            json.dumps(
                                updated,
                                ensure_ascii=False,
                            ),
                    }
                )

                processed += 1

                print(
                    f"Ответ обработан: "
                    f"{doctor['name']} "
                    f"[{bot_id}]"
                )

            except Exception as exc:

                print(
                    "IMAP: ошибка обработки "
                    f"сообщения: {exc}"
                )

        return processed

    except Exception as exc:

        print(
            f"IMAP ERROR: {exc}"
        )

        return processed

    finally:

        if mail is not None:

            try:

                print(
                    "IMAP: закрытие соединения..."
                )

                try:
                    mail.close()
                except Exception:
                    pass

                try:
                    mail.logout()
                except Exception:
                    pass

                print(
                    "IMAP: соединение закрыто."
                )

            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        f"BOTON v{VERSION}"
    )

    print(
        f"TEST_MODE = {TEST_MODE}"
    )

    print(
        f"TEST_RECEIVE_ONLY = "
        f"{TEST_RECEIVE_ONLY}"
    )

    target_date = now_moscow().date()

    print(
        "Дата: "
        f"{target_date.strftime('%d.%m.%Y')}"
    )

    print(
        "Текущее время МСК: "
        f"{now_moscow().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    # --------------------------------------------------------
    # GOOGLE SHEETS
    # --------------------------------------------------------

    (
        tasks_sheet,
        doctors_sheet,
        tasks_book,
    ) = open_sheet()

    log_sheet = ensure_log_sheet(
        tasks_book
    )

    print(
        f"BoTon_Log: "
        f"{log_sheet.title}"
    )

    # --------------------------------------------------------
    # DOCTORS
    # --------------------------------------------------------

    doctors = load_doctors(
        doctors_sheet
    )

    # --------------------------------------------------------
    # TASKS
    # --------------------------------------------------------

    rows, stats = load_task_rows(
        tasks_sheet,
        target_date,
    )

    print(
        f"Прочитано строк: "
        f"{stats['total']}"
    )

    print(
        f"Пустых строк: "
        f"{stats['empty']}"
    )

    print(
        f"Строк с нераспознанной датой: "
        f"{stats['bad_date']}"
    )

    print(
        f"Строк с другой датой: "
        f"{stats['other_date']}"
    )

    print(
        f"Строк других филиалов: "
        f"{stats['other_branch']}"
    )

    print(
        f"Уже обработано по M: "
        f"{stats['already_processed']}"
    )

    print(
        f"Без ФИО врача: "
        f"{stats['no_doctor']}"
    )

    print(
        f"Без заданий: "
        f"{stats['no_tasks']}"
    )

    print(
        f"Подошло для обработки: "
        f"{len(rows)}"
    )

    # --------------------------------------------------------
    # INCOMING — 24/7
    # --------------------------------------------------------

    received = process_incoming_messages(
        tasks_sheet,
        log_sheet,
        doctors,
    )

    print(
        f"Обработано входящих ответов: "
        f"{received}"
    )

    # --------------------------------------------------------
    # SEND ASSIGNMENTS
    # ONLY 08:00–12:00 MOSCOW
    # --------------------------------------------------------

    if not TEST_RECEIVE_ONLY:

        if is_assignment_time():

            print(
                "\n----- РАССЫЛКА -----"
            )

            print(
                "Рассылка разрешена: "
                "08:00–12:00 МСК"
            )

            send_today_assignments(
                tasks_sheet,
                log_sheet,
                doctors,
                rows,
                target_date,
            )

        else:

            print(
                "\n----- РАССЫЛКА НЕ ВЫПОЛНЯЕТСЯ -----"
            )

            print(
                f"Текущее время МСК: "
                f"{now_moscow().strftime('%d.%m.%Y %H:%M:%S')}"
            )

            print(
                "Рассылка заданий разрешена "
                "только с 08:00 до 12:00 МСК."
            )

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

    print(
        "\n----- ИТОГОВАЯ СТАТИСТИКА -----"
    )

    grouped = group_by_doctor(
        rows
    )

    total_tasks = sum(
        len(item["tasks"])
        for item in rows
    )

    print(
        f"Врачей: "
        f"{len(grouped)}"
    )

    print(
        f"Пациентов: "
        f"{len(rows)}"
    )

    print(
        f"Заданий: "
        f"{total_tasks}"
    )

    print(
        f"Входящих обработано: "
        f"{received}"
    )

    print(
        f"BOTON v{VERSION} завершён."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
