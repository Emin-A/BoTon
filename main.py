# ============================================================
# BoTon v2.2
# Рассылка заданий + приём ответов врачей + запись H/J/L
# + точная привязка ответа к отправленному заданию
# + защищённый IMAP без STORE/UNSEEN
# + журнал BoTon_Log
#
# VERSION: 2.2
# ============================================================

import os
import re
import ssl
import imaplib
import smtplib
import json
import secrets

from collections import defaultdict
from datetime import datetime, timedelta
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

VERSION = "2.2"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ------------------------------------------------------------
# Google Sheets
# ------------------------------------------------------------

TASKS_SPREADSHEET_ID = (
    "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
)

TASKS_SHEET_NAME = "Лист1"

DOCTORS_SPREADSHEET_ID = (
    "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
)

DOCTORS_SHEET_NAME = "Врачи"

# Журнал отправлений и входящих ответов
LOG_SHEET_NAME = "BoTon_Log"


# ------------------------------------------------------------
# Колонки базы врачей
# ------------------------------------------------------------

COL_DOCTOR_NAME = 0
COL_DOCTOR_CHAT_ID = 1
COL_DOCTOR_EMAIL = 2


# ------------------------------------------------------------
# Колонки таблицы заданий
# ------------------------------------------------------------

COL_DATE = 0
COL_BRANCH = 1
COL_DOCTOR = 2
COL_OMS = 3
COL_PATIENT = 4
COL_SOURCE = 5

COL_TASK1 = 6
COL_DONE1 = 7

COL_TASK2 = 8
COL_DONE2 = 9

COL_TASK3 = 10
COL_DONE3 = 11

COL_SEND_STATUS = 12


# ============================================================
# ENV / SECRETS
# ============================================================

GOOGLE_SERVICE_ACCOUNT = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT",
    ""
)

SMTP_HOST = os.environ.get(
    "SMTP_HOST",
    "smtp.yandex.ru"
)

SMTP_PORT = int(
    os.environ.get(
        "SMTP_PORT",
        "465"
    )
)

SMTP_USER = os.environ.get(
    "SMTP_USER",
    ""
)

SMTP_PASSWORD = os.environ.get(
    "SMTP_PASSWORD",
    ""
)

SMTP_FROM_EMAIL = os.environ.get(
    "SMTP_FROM_EMAIL",
    SMTP_USER
)


# ------------------------------------------------------------
# IMAP
# ------------------------------------------------------------

IMAP_HOST = os.environ.get(
    "IMAP_HOST",
    "imap.yandex.ru"
)

IMAP_PORT = int(
    os.environ.get(
        "IMAP_PORT",
        "993"
    )
)

IMAP_USER = os.environ.get(
    "IMAP_USER",
    SMTP_USER
)

IMAP_PASSWORD = os.environ.get(
    "IMAP_PASSWORD",
    SMTP_PASSWORD
)

# Timeout IMAP
IMAP_TIMEOUT = 20

# Искать ответы только за последние N дней
IMAP_LOOKBACK_DAYS = 3

# Максимум писем от одного врача за один запуск
IMAP_MAX_PER_DOCTOR = 20

# Максимум всех писем за один запуск
IMAP_MAX_TOTAL = 100


# ------------------------------------------------------------
# Резервная почта
# ------------------------------------------------------------

RESERVE_EMAIL = "eagaverdiev@mosmedzdrav.ru"


# ------------------------------------------------------------
# Филиал 2
# ------------------------------------------------------------

BRANCH_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]


# ------------------------------------------------------------
# Режимы
# ------------------------------------------------------------

TEST_MODE = False

TEST_RECEIVE_ONLY = False


# ============================================================
# СЛУЖЕБНЫЕ ФУНКЦИИ
# ============================================================

def now_moscow():
    return datetime.now(MOSCOW_TZ)


def today_moscow():
    return now_moscow().date()


def today_str():
    return today_moscow().strftime("%d.%m.%Y")


def clean(value):
    if value is None:
        return ""

    value = str(value).strip()

    if value.lower() in {
        "null",
        "undefined"
    }:
        return ""

    return value


def normalize_text(value):
    value = clean(value).lower()
    value = value.replace("ё", "е")

    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def normalize_branch(value):
    return normalize_text(value)


def is_target_branch(branch):
    branch = normalize_branch(branch)

    return any(
        normalize_text(keyword) in branch
        for keyword in BRANCH_KEYWORDS
    )


# ============================================================
# ДАТА
# ============================================================

def parse_date(value):
    value = clean(value)

    if not value:
        return None

    formats = [
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d.%m.%y",
    ]

    for fmt in formats:

        try:
            return datetime.strptime(
                value,
                fmt
            ).date()

        except ValueError:
            pass

    return None


# ============================================================
# ФИО ВРАЧА
# ============================================================

def get_signature(name):

    name = normalize_text(name)

    if not name:
        return ""

    parts = name.split()

    if not parts:
        return ""

    # О.В. Нецветаева
    if (
        len(parts) >= 2
        and re.fullmatch(
            r"[а-я]\.?[а-я]?\.?",
            parts[0]
        )
    ):

        surname = parts[1]

        initials = re.sub(
            r"[^а-я]",
            "",
            parts[0]
        )

        return f"{surname} {initials}".strip()

    surname = parts[0]

    initials = ""

    for part in parts[1:]:

        letters = re.sub(
            r"[^а-я]",
            "",
            part
        )

        if letters:
            initials += letters[0]

    return f"{surname} {initials}".strip()


def surname_of_signature(signature):

    return (
        signature.split()[0]
        if signature
        else ""
    )


def initials_of_signature(signature):

    parts = signature.split()

    return (
        parts[1]
        if len(parts) > 1
        else ""
    )


def find_doctor(contact_name, doctors_map):

    signature = get_signature(
        contact_name
    )

    if not signature:
        return None

    if signature in doctors_map:
        return doctors_map[signature]

    surname = surname_of_signature(
        signature
    )

    initials = initials_of_signature(
        signature
    )

    for key, doctor in doctors_map.items():

        db_surname = surname_of_signature(
            key
        )

        db_initials = initials_of_signature(
            key
        )

        if db_surname != surname:
            continue

        if not initials or not db_initials:
            return doctor

        if (
            initials.startswith(db_initials)
            or db_initials.startswith(initials)
        ):
            return doctor

    if len(surname) >= 4:

        for key, doctor in doctors_map.items():

            db_surname = surname_of_signature(
                key
            )

            if (
                surname in db_surname
                or db_surname in surname
            ):
                return doctor

    return None


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_google_client():

    if not GOOGLE_SERVICE_ACCOUNT:

        raise RuntimeError(
            "Не задан GOOGLE_SERVICE_ACCOUNT"
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials_info = json.loads(
        GOOGLE_SERVICE_ACCOUNT
    )

    credentials = (
        Credentials.from_service_account_info(
            credentials_info,
            scopes=scopes
        )
    )

    return gspread.authorize(
        credentials
    )


def ensure_log_sheet(tasks_ss):

    try:

        log_sheet = tasks_ss.worksheet(
            LOG_SHEET_NAME
        )

        print(
            f"Google Sheets: лист "
            f"{LOG_SHEET_NAME} найден"
        )

    except Exception:

        print(
            f"Google Sheets: создаю лист "
            f"{LOG_SHEET_NAME}"
        )

        log_sheet = tasks_ss.add_worksheet(
            title=LOG_SHEET_NAME,
            rows=2000,
            cols=10
        )

        log_sheet.append_row(
            [
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
            ],
            value_input_option="USER_ENTERED"
        )

    return log_sheet


def get_sheets():

    client = get_google_client()

    tasks_ss = client.open_by_key(
        TASKS_SPREADSHEET_ID
    )

    tasks_sheet = tasks_ss.worksheet(
        TASKS_SHEET_NAME
    )

    doctors_ss = client.open_by_key(
        DOCTORS_SPREADSHEET_ID
    )

    doctors_sheet = doctors_ss.worksheet(
        DOCTORS_SHEET_NAME
    )

    log_sheet = ensure_log_sheet(
        tasks_ss
    )

    return (
        tasks_ss,
        tasks_sheet,
        doctors_sheet,
        log_sheet,
    )


# ============================================================
# БАЗА ВРАЧЕЙ
# ============================================================

def get_doctors_map(doctors_sheet):

    data = doctors_sheet.get_all_values()

    doctors = {}

    for row in data[1:]:

        if not row:
            continue

        name = clean(
            row[COL_DOCTOR_NAME]
            if len(row) > COL_DOCTOR_NAME
            else ""
        )

        chat_id = clean(
            row[COL_DOCTOR_CHAT_ID]
            if len(row) > COL_DOCTOR_CHAT_ID
            else ""
        )

        email = clean(
            row[COL_DOCTOR_EMAIL]
            if len(row) > COL_DOCTOR_EMAIL
            else ""
        )

        if not name:
            continue

        signature = get_signature(
            name
        )

        if not signature:
            continue

        doctors[signature] = {
            "name": name,
            "chat_id": chat_id,
            "email": email,
        }

    return doctors


# ============================================================
# ЗАДАНИЯ
# ============================================================

TASK_DEFINITIONS = [
    {
        "task_col": COL_TASK1,
        "status_col": COL_DONE1,
        "title": "Задание 1 (липиды)",
    },
    {
        "task_col": COL_TASK2,
        "status_col": COL_DONE2,
        "title": "Задание 2 (КДП — выгрузка)",
    },
    {
        "task_col": COL_TASK3,
        "status_col": COL_DONE3,
        "title": "Задание 3 (КДП СД 3 квартал)",
    },
]


def make_send_status(email):

    return (
        f"✅ Отправлено "
        f"{now_moscow().strftime('%d.%m.%Y %H:%M')} "
        f"на {email}"
    )


def make_error_status():

    return (
        f"❌ Ошибка отправки "
        f"{now_moscow().strftime('%d.%m.%Y %H:%M')}"
    )


def get_tasks_from_row(
    row,
    row_number
):

    result = []

    for definition in TASK_DEFINITIONS:

        task_col = definition["task_col"]

        task_text = clean(
            row[task_col]
            if len(row) > task_col
            else ""
        )

        if not task_text:
            continue

        result.append({
            "row_number": row_number,
            "task_col": task_col,
            "status_col": definition["status_col"],
            "title": definition["title"],
            "text": task_text,
        })

    return result


# ============================================================
# ПАЦИЕНТ
# ============================================================

def patient_key(row):

    oms = clean(
        row[COL_OMS]
        if len(row) > COL_OMS
        else ""
    )

    patient = clean(
        row[COL_PATIENT]
        if len(row) > COL_PATIENT
        else ""
    )

    return (
        oms
        or normalize_text(patient)
    )


def group_assignment_rows(items):

    patients = {}

    for item in items:

        row = item["row"]
        row_number = item["row_number"]

        key = patient_key(row)

        if key not in patients:

            patients[key] = {
                "patient": clean(
                    row[COL_PATIENT]
                ),
                "oms": clean(
                    row[COL_OMS]
                ),
                "tasks": [],
                "rows": [],
            }

        patients[key]["rows"].append(
            row_number
        )

        patients[key]["tasks"].extend(
            item["tasks"]
        )

    return list(
        patients.values()
    )


# ============================================================
# БУКВЫ ЗАДАНИЙ
# ============================================================

TASK_LETTERS = [
    "А",
    "Б",
    "В",
    "Г",
    "Д",
    "Е",
    "Ж",
    "З",
    "И",
    "К",
    "Л",
]


def task_label(
    patient_number,
    task_index,
    total_tasks
):

    if total_tasks == 1:
        return str(patient_number)

    return (
        f"{patient_number}"
        f"{TASK_LETTERS[task_index]}"
    )


# ============================================================
# ПРИВЕТСТВИЕ
# ============================================================

def extract_first_name_patronymic(
    full_name
):

    name = clean(full_name)

    parts = name.split()

    if len(parts) >= 3:

        return (
            f"{parts[1]} "
            f"{parts[2]}"
        )

    return name


def make_greeting(doctor):

    display_name = (
        doctor.get("full_name")
        or extract_first_name_patronymic(
            doctor["name"]
        )
    )

    return (
        f"Уважаемая(ый) {display_name}!"
    )


# ============================================================
# BOTON-ID
# ============================================================

def make_doctor_key(doctor):

    signature = get_signature(
        doctor["name"]
    )

    return re.sub(
        r"[^a-zа-я0-9]+",
        "-",
        signature
    ).strip("-")


def make_bot_id(
    target_date,
    doctor
):

    # Уникальный ID каждого конкретного отправления.
    token = secrets.token_hex(
        2
    ).upper()

    return (
        f"BOTON-"
        f"{target_date.strftime('%Y%m%d')}-"
        f"{make_doctor_key(doctor)}-"
        f"{token}"
    )


def extract_bot_ids(text):

    return re.findall(
        r"\[BOTON-ID:([^\]]+)\]",
        text,
        flags=re.IGNORECASE
    )


def extract_bot_id(text):

    ids = extract_bot_ids(text)

    return (
        ids[0].strip()
        if ids
        else ""
    )


# ============================================================
# ПИСЬМО
# ============================================================

def build_email_body(
    doctor,
    patients,
    target_date,
    bot_id,
):

    lines = []

    lines.append(
        make_greeting(doctor)
    )

    lines.append("")

    lines.append(
        "Вам необходимо выполнить важные задания "
        "по записанным на сегодня к вам пациентам:"
    )

    lines.append("")

    total_tasks = 0

    for patient_number, patient in enumerate(
        patients,
        start=1
    ):

        tasks = patient["tasks"]

        total_tasks += len(tasks)

        lines.append(
            f"{patient_number}. "
            f"{patient['patient']}"
        )

        if patient["oms"]:

            lines.append(
                f"ID/ОМС: {patient['oms']}"
            )

        lines.append("")

        multiple = len(tasks) > 1

        for task_index, task in enumerate(
            tasks
        ):

            label = task_label(
                patient_number,
                task_index,
                len(tasks)
            )

            if multiple:

                lines.append(
                    f"   {label[-1]}. "
                    f"{task['title']}:"
                )

                lines.append(
                    f"      {task['text']}"
                )

            else:

                lines.append(
                    f"{task['title']}:"
                )

                lines.append(
                    f"{task['text']}"
                )

            lines.append("")

        lines.append("---")
        lines.append("")

    lines.append(
        f"Всего пациентов: {len(patients)}"
    )

    lines.append(
        f"Всего заданий: {total_tasks}"
    )

    lines.append(
        "Срок выполнения: сегодня"
    )

    lines.append("")

    lines.append(
        "Ответьте на это письмо, указав "
        "выполненные и невыполненные задания."
    )

    lines.append("")

    lines.append(
        "Примеры ответа:"
    )

    lines.append(
        "Выполнено: 1, 2, 3"
    )

    lines.append(
        "Выполнено: 1, 2А, 3"
    )

    lines.append(
        "Не выполнено: 2Б"
    )

    lines.append(
        "Частично: 3Б"
    )

    lines.append("")

    lines.append(
        "Если у пациента несколько заданий, "
        "указание только номера пациента означает, "
        "что выполнены все его задания."
    )

    lines.append("")

    lines.append(
        "Прошу вернуть отработанное задание "
        "вашему заведующему."
    )

    lines.append("")

    lines.append(
        "С уважением,"
    )

    lines.append(
        "BoTon и Агаввердиев Э.М."
    )

    lines.append(
        now_moscow().strftime(
            "%d.%m.%Y %H:%M"
        )
    )

    lines.append("")

    lines.append(
        f"[BOTON-ID:{bot_id}]"
    )

    return "\n".join(lines)


# ============================================================
# SMTP
# ============================================================

def smtp_connect():

    print(
        f"SMTP: подключение к "
        f"{SMTP_HOST}:{SMTP_PORT}..."
    )

    context = ssl.create_default_context()

    server = smtplib.SMTP_SSL(
        SMTP_HOST,
        SMTP_PORT,
        context=context,
        timeout=30,
    )

    print(
        "SMTP: SSL OK"
    )

    server.login(
        SMTP_USER,
        SMTP_PASSWORD,
    )

    print(
        "SMTP: авторизация OK"
    )

    return server


def send_email(
    smtp,
    recipient,
    subject,
    body,
):

    message = EmailMessage()

    message["From"] = SMTP_FROM_EMAIL
    message["To"] = recipient
    message["Subject"] = subject

    message.set_content(
        body,
        charset="utf-8"
    )

    smtp.send_message(
        message
    )


# ============================================================
# IMAP
# ============================================================

def decode_mime_header(value):

    if not value:
        return ""

    result = []

    for part, encoding in decode_header(value):

        if isinstance(part, bytes):

            try:

                result.append(
                    part.decode(
                        encoding or "utf-8",
                        errors="replace"
                    )
                )

            except Exception:

                result.append(
                    part.decode(
                        "utf-8",
                        errors="replace"
                    )
                )

        else:

            result.append(
                str(part)
            )

    return "".join(result)


def get_email_body(message):

    if message.is_multipart():

        plain_parts = []

        for part in message.walk():

            content_type = (
                part.get_content_type()
            )

            disposition = str(
                part.get(
                    "Content-Disposition"
                )
                or ""
            )

            if (
                content_type == "text/plain"
                and "attachment"
                not in disposition.lower()
            ):

                try:

                    text = part.get_content()

                except Exception:

                    payload = part.get_payload(
                        decode=True
                    )

                    if not payload:
                        continue

                    charset = (
                        part.get_content_charset()
                        or "utf-8"
                    )

                    text = payload.decode(
                        charset,
                        errors="replace"
                    )

                plain_parts.append(text)

        if plain_parts:

            return "\n".join(
                plain_parts
            )

    try:

        return message.get_content()

    except Exception:

        payload = message.get_payload(
            decode=True
        )

        if not payload:
            return ""

        return payload.decode(
            "utf-8",
            errors="replace"
        )


def connect_imap():

    print(
        f"IMAP: подключение к "
        f"{IMAP_HOST}:{IMAP_PORT}..."
    )

    mail = imaplib.IMAP4_SSL(
        IMAP_HOST,
        IMAP_PORT,
        timeout=IMAP_TIMEOUT,
    )

    print(
        "IMAP: SSL-соединение установлено"
    )

    try:

        if getattr(mail, "sock", None):

            mail.sock.settimeout(
                IMAP_TIMEOUT
            )

    except Exception as error:

        print(
            f"IMAP: timeout сокета "
            f"не установлен: {error}"
        )

    print(
        f"IMAP: авторизация пользователя "
        f"{IMAP_USER}..."
    )

    mail.login(
        IMAP_USER,
        IMAP_PASSWORD
    )

    print(
        "IMAP: авторизация OK"
    )

    print(
        "IMAP: открытие INBOX..."
    )

    # ВАЖНО:
    # readonly=True.
    # Никакого STORE в v2.2 нет вообще.
    status, data = mail.select(
        "INBOX",
        readonly=True
    )

    if status != "OK":

        raise RuntimeError(
            f"IMAP: не удалось открыть INBOX: "
            f"{data}"
        )

    print(
        "IMAP: INBOX открыт в режиме readonly"
    )

    return mail


# ============================================================
# ЖУРНАЛ BoTon_Log
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


def get_log_rows(log_sheet):

    try:

        rows = log_sheet.get_all_values()

    except Exception as error:

        print(
            f"BoTon_Log: ошибка чтения: {error}"
        )

        return []

    if not rows:
        return []

    return rows


def append_log(
    log_sheet,
    event,
    bot_id,
    target_date,
    doctor_name="",
    email="",
    mapping=None,
    message_id="",
    result="",
    notes="",
):

    mapping_json = ""

    if mapping is not None:

        mapping_json = json.dumps(
            mapping,
            ensure_ascii=False,
            separators=(",", ":")
        )

    log_sheet.append_row(
        [
            now_moscow().strftime(
                "%d.%m.%Y %H:%M:%S"
            ),
            event,
            bot_id,
            (
                target_date.strftime("%d.%m.%Y")
                if target_date
                else ""
            ),
            doctor_name,
            email,
            mapping_json,
            message_id,
            result,
            notes,
        ],
        value_input_option="USER_ENTERED"
    )


def find_send_log(
    log_rows,
    bot_id,
):

    # Ищем последнее SEND с таким BOTON-ID.
    for row_number in range(
        len(log_rows) - 1,
        0,
        -1
    ):

        row = log_rows[row_number]

        if len(row) < 10:
            continue

        event = clean(row[1])
        current_bot_id = clean(row[2])

        if (
            event == "SEND"
            and current_bot_id == bot_id
        ):

            mapping_json = clean(
                row[6]
            )

            try:

                mapping = json.loads(
                    mapping_json
                )

            except Exception:

                mapping = {}

            target_date = parse_date(
                row[3]
            )

            return {
                "row_number": row_number + 1,
                "bot_id": bot_id,
                "target_date": target_date,
                "doctor_name": clean(row[4]),
                "email": clean(row[5]).lower(),
                "mapping": mapping,
                "result": clean(row[8]),
            }

    return None


def message_was_processed(
    log_rows,
    message_id,
):

    if not message_id:
        return False

    for row in log_rows[1:]:

        if len(row) < 8:
            continue

        if (
            clean(row[1]) == "RECEIVE"
            and clean(row[7]) == message_id
        ):
            return True

    return False


def update_log_result(
    log_sheet,
    log_row_number,
    result,
):

    try:

        log_sheet.update_cell(
            log_row_number,
            9,
            result
        )

    except Exception as error:

        print(
            f"BoTon_Log: ошибка обновления "
            f"строки {log_row_number}: "
            f"{error}"
        )


# ============================================================
# КАРТА ТОЧНЫХ ЗАДАНИЙ
# ============================================================

def build_exact_response_task_map(
    patients
):

    mapping = defaultdict(list)

    for patient_number, patient in enumerate(
        patients,
        start=1
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

            mapping[key].append({
                "row_number": task["row_number"],
                "status_col": task["status_col"],
                "task_col": task["task_col"],
                "title": task["title"],
            })

    return dict(mapping)


def build_mapping_for_log(
    patients
):

    return build_exact_response_task_map(
        patients
    )


# ============================================================
# РАЗБОР ОТВЕТА
# ============================================================

STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_NOT_DONE = "not_done"


def extract_reply_text(text):

    """
    Убирает процитированное исходное письмо.

    Это важно:
    в исходном письме есть примеры
    'Выполнено: 1, 2, 3'.
    Их нельзя принимать за ответ врача.
    """

    text = text.replace(
        "\r\n",
        "\n"
    ).replace(
        "\r",
        "\n"
    )

    lines = []

    quote_markers = [
        "-----original message-----",
        "original message",
        "исходное сообщение",
        "начало цитируемого сообщения",
        "begin forwarded message",
    ]

    for line in text.split("\n"):

        stripped = line.strip()

        lower = stripped.lower()

        if any(
            marker in lower
            for marker in quote_markers
        ):
            break

        if stripped.startswith(">"):
            continue

        # Outlook / Gmail:
        # On ... wrote:
        if re.match(
            r"^on .+ wrote:\s*$",
            stripped,
            flags=re.IGNORECASE
        ):
            break

        if re.match(
            r"^в .+ написал(?:а|о)?:\s*$",
            stripped,
            flags=re.IGNORECASE
        ):
            break

        lines.append(line)

    return "\n".join(lines).strip()


def normalize_answer_text(text):

    text = text.replace(
        "\r",
        "\n"
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    return text


def extract_task_tokens(text):

    """
    Извлекает:
    1
    2А
    2Б
    15В
    """

    tokens = re.findall(
        r"\b(\d+)\s*([А-ЯЁа-яё]?)\b",
        text
    )

    result = []

    for number, letter in tokens:

        try:
            number = int(number)

        except ValueError:
            continue

        letter = (
            letter.upper()
            if letter
            else ""
        )

        result.append(
            (number, letter)
        )

    return result


def parse_status_sections(text):

    text = extract_reply_text(
        text
    )

    text = normalize_answer_text(
        text
    )

    sections = {
        STATUS_DONE: [],
        STATUS_PARTIAL: [],
        STATUS_NOT_DONE: [],
    }

    # --------------------------------------------------------
    # Формат:
    # Выполнено: 1, 2А
    # Не выполнено: 3Б
    # Частично: 4
    # --------------------------------------------------------

    line_patterns = [
        (
            STATUS_NOT_DONE,
            r"^\s*не\s+выполнен(?:о|ы)"
            r"\s*[:\-]?\s*(.+?)\s*$",
        ),
        (
            STATUS_PARTIAL,
            r"^\s*частично"
            r"\s*[:\-]?\s*(.+?)\s*$",
        ),
        (
            STATUS_DONE,
            r"^\s*выполнен(?:о|ы)"
            r"\s*[:\-]?\s*(.+?)\s*$",
        ),
    ]

    for line in text.split("\n"):

        line = line.strip()

        if not line:
            continue

        for status, pattern in line_patterns:

            match = re.match(
                pattern,
                line,
                flags=re.IGNORECASE
            )

            if match:

                tokens = extract_task_tokens(
                    match.group(1)
                )

                sections[status].extend(
                    tokens
                )

                break

        # ----------------------------------------------------
        # Формат:
        # 2А — выполнено
        # 3Б — частично
        # 4 — не выполнено
        # ----------------------------------------------------

        item_first = re.match(
            r"^\s*(.+?)\s*"
            r"(?:—|–|-|:)\s*"
            r"(не\s+выполнено|"
            r"частично|"
            r"выполнено)"
            r"\s*$",
            line,
            flags=re.IGNORECASE
        )

        if item_first:

            item_text = item_first.group(1)
            status_text = (
                item_first.group(2)
                .lower()
            )

            tokens = extract_task_tokens(
                item_text
            )

            if "не выполнено" in status_text:

                status = STATUS_NOT_DONE

            elif "частично" in status_text:

                status = STATUS_PARTIAL

            else:

                status = STATUS_DONE

            sections[status].extend(
                tokens
            )

    return sections


# ============================================================
# ПРЕОБРАЗОВАНИЕ СТАТУСОВ В КЛЮЧИ
# ============================================================

def expand_status_items(
    items,
    mapping
):

    result = set()

    for patient_number, letter in items:

        # Номер без буквы:
        # найти все задания пациента.
        if not letter:

            prefix = f"{patient_number}"

            for key in mapping:

                if (
                    key == prefix
                    or (
                        key.startswith(prefix)
                        and key[len(prefix):]
                    )
                ):

                    result.add(key)

        else:

            key = (
                f"{patient_number}"
                f"{letter}"
            )

            if key in mapping:

                result.add(key)

    return result


# ============================================================
# ПРИМЕНЕНИЕ ОТВЕТА ПО ТОЧНОЙ КАРТЕ
# ============================================================

def apply_doctor_response(
    worksheet,
    mapping,
    answer_text,
):

    sections = parse_status_sections(
        answer_text
    )

    done = expand_status_items(
        sections[STATUS_DONE],
        mapping
    )

    partial = expand_status_items(
        sections[STATUS_PARTIAL],
        mapping
    )

    not_done = expand_status_items(
        sections[STATUS_NOT_DONE],
        mapping
    )

    # Приоритет:
    # Не выполнено > Частично > Выполнено

    partial -= not_done
    done -= not_done
    done -= partial

    timestamp = now_moscow().strftime(
        "%d.%m.%Y %H:%M"
    )

    updates = []

    statistics = {
        "done": 0,
        "partial": 0,
        "not_done": 0,
    }

    def status_text(status):

        if status == STATUS_DONE:

            return (
                f"✅ Выполнено {timestamp}"
            )

        if status == STATUS_PARTIAL:

            return (
                f"⚠️ Частично {timestamp}"
            )

        return (
            f"❌ Не выполнено {timestamp}"
        )

    # --------------------------------------------------------
    # Каждая запись mapping может содержать несколько строк.
    # --------------------------------------------------------

    for key, task_refs in mapping.items():

        if key in done:

            status = STATUS_DONE

        elif key in partial:

            status = STATUS_PARTIAL

        elif key in not_done:

            status = STATUS_NOT_DONE

        else:

            continue

        for task in task_refs:

            row_number = task["row_number"]
            status_col = task["status_col"]

            cell = (
                gspread.utils.rowcol_to_a1(
                    row_number,
                    status_col + 1
                )
            )

            updates.append({
                "range": cell,
                "values": [
                    [status_text(status)]
                ],
            })

            statistics[status] += 1

    if updates and not TEST_MODE:

        worksheet.batch_update(
            updates,
            value_input_option="USER_ENTERED"
        )

    return statistics, updates


# ============================================================
# ПАЦИЕНТЫ ВРАЧА
# ============================================================

def get_patients_for_doctor(
    rows,
    doctor,
    target_date,
    include_processed=False,
):

    doctor_signature = get_signature(
        doctor["name"]
    )

    patients = {}

    for row_number, row in enumerate(
        rows,
        start=2
    ):

        if len(row) <= COL_DOCTOR:
            continue

        row_date = parse_date(
            row[COL_DATE]
            if len(row) > COL_DATE
            else ""
        )

        if row_date != target_date:
            continue

        if not is_target_branch(
            row[COL_BRANCH]
            if len(row) > COL_BRANCH
            else ""
        ):
            continue

        row_doctor = clean(
            row[COL_DOCTOR]
        )

        matched = find_doctor(
            row_doctor,
            {
                doctor_signature: doctor
            }
        )

        if not matched:
            continue

        send_status = clean(
            row[COL_SEND_STATUS]
            if len(row) > COL_SEND_STATUS
            else ""
        )

        if (
            send_status
            and not include_processed
        ):
            continue

        tasks = get_tasks_from_row(
            row,
            row_number
        )

        if not tasks:
            continue

        key = patient_key(row)

        if key not in patients:

            patients[key] = {
                "patient": clean(
                    row[COL_PATIENT]
                ),
                "oms": clean(
                    row[COL_OMS]
                ),
                "tasks": [],
                "rows": [],
            }

        patients[key]["tasks"].extend(
            tasks
        )

        patients[key]["rows"].append(
            row_number
        )

    return list(
        patients.values()
    )


# ============================================================
# РАССЫЛКА
# ============================================================

def send_today_assignments(
    worksheet,
    log_sheet,
    doctors_map,
    rows,
    target_date,
):

    assignments = defaultdict(list)

    for row_number, row in enumerate(
        rows,
        start=2
    ):

        row_date = parse_date(
            row[COL_DATE]
            if len(row) > COL_DATE
            else ""
        )

        if row_date != target_date:
            continue

        branch = (
            row[COL_BRANCH]
            if len(row) > COL_BRANCH
            else ""
        )

        if not is_target_branch(branch):
            continue

        doctor_name = clean(
            row[COL_DOCTOR]
            if len(row) > COL_DOCTOR
            else ""
        )

        if not doctor_name:
            continue

        doctor = find_doctor(
            doctor_name,
            doctors_map
        )

        if not doctor:
            continue

        send_status = clean(
            row[COL_SEND_STATUS]
            if len(row) > COL_SEND_STATUS
            else ""
        )

        if send_status:
            continue

        tasks = get_tasks_from_row(
            row,
            row_number
        )

        if not tasks:
            continue

        key = get_signature(
            doctor["name"]
        )

        assignments[key].append({
            "row_number": row_number,
            "row": row,
            "doctor": doctor,
            "tasks": tasks,
        })

    if not assignments:

        print(
            "Новых заданий для рассылки нет."
        )

        return

    smtp = None

    try:

        smtp = smtp_connect()

        print(
            "SMTP: OK"
        )

        for doctor_key, items in assignments.items():

            doctor = items[0]["doctor"]

            email = clean(
                doctor.get("email")
            )

            if not email:

                print(
                    f"Нет email у врача: "
                    f"{doctor['name']}"
                )

                continue

            patients = group_assignment_rows(
                items
            )

            if not patients:
                continue

            # ------------------------------------------------
            # Уникальный BOTON-ID
            # ------------------------------------------------

            bot_id = make_bot_id(
                target_date,
                doctor
            )

            # ------------------------------------------------
            # ТОЧНАЯ КАРТА
            # ------------------------------------------------

            mapping = build_mapping_for_log(
                patients
            )

            subject = (
                f"BoTon — задания "
                f"на "
                f"{target_date.strftime('%d.%m.%Y')} "
                f"[{bot_id}]"
            )

            body = build_email_body(
                doctor,
                patients,
                target_date,
                bot_id,
            )

            if (
                TEST_MODE
                or TEST_RECEIVE_ONLY
            ):

                print(
                    "\n===== TEST EMAIL ====="
                )

                print(
                    f"To: {email}"
                )

                print(
                    f"Subject: {subject}"
                )

                print("")

                print(body)

                print(
                    "\nТочная карта:"
                )

                print(
                    json.dumps(
                        mapping,
                        ensure_ascii=False,
                        indent=2
                    )
                )

                print(
                    "======================"
                )

                continue

            # ------------------------------------------------
            # Сначала создаём запись PENDING.
            #
            # Это гарантирует, что карта задания существует
            # ещё до фактической отправки письма.
            # ------------------------------------------------

            try:

                append_log(
                    log_sheet,
                    event="SEND",
                    bot_id=bot_id,
                    target_date=target_date,
                    doctor_name=doctor["name"],
                    email=email,
                    mapping=mapping,
                    result="PENDING",
                    notes="Карта создана до отправки",
                )

                log_rows = get_log_rows(
                    log_sheet
                )

                send_log = find_send_log(
                    log_rows,
                    bot_id
                )

                if not send_log:

                    raise RuntimeError(
                        "Не удалось найти созданную "
                        "запись SEND в BoTon_Log"
                    )

                send_log_row = send_log[
                    "row_number"
                ]

            except Exception as error:

                print(
                    f"BoTon_Log: не удалось создать "
                    f"карту для {doctor['name']}: "
                    f"{error}"
                )

                continue

            # ------------------------------------------------
            # Отправка
            # ------------------------------------------------

            try:

                send_email(
                    smtp,
                    email,
                    subject,
                    body,
                )

                status = make_send_status(
                    email
                )

                updates = []

                for item in items:

                    cell = (
                        gspread.utils
                        .rowcol_to_a1(
                            item["row_number"],
                            COL_SEND_STATUS + 1
                        )
                    )

                    updates.append({
                        "range": cell,
                        "values": [[status]],
                    })

                if updates:

                    worksheet.batch_update(
                        updates,
                        value_input_option="USER_ENTERED"
                    )

                update_log_result(
                    log_sheet,
                    send_log_row,
                    "SENT"
                )

                print(
                    f"Письмо врачу отправлено: "
                    f"{doctor['name']} "
                    f"({email})"
                )

                print(
                    f"BOTON-ID: {bot_id}"
                )

                print(
                    f"Точная карта: "
                    f"{len(mapping)} пунктов"
                )

            except Exception as error:

                print(
                    f"Ошибка отправки "
                    f"{doctor['name']}: "
                    f"{error}"
                )

                update_log_result(
                    log_sheet,
                    send_log_row,
                    "SEND_ERROR"
                )

                if not TEST_MODE:

                    updates = []

                    status = make_error_status()

                    for item in items:

                        cell = (
                            gspread.utils
                            .rowcol_to_a1(
                                item["row_number"],
                                COL_SEND_STATUS + 1
                            )
                        )

                        updates.append({
                            "range": cell,
                            "values": [[status]],
                        })

                    if updates:

                        worksheet.batch_update(
                            updates,
                            value_input_option="USER_ENTERED"
                        )

    finally:

        if smtp:

            try:
                smtp.quit()

            except Exception:
                pass


# ============================================================
# ПОИСК BOTON-ID В ПИСЬМЕ
# ============================================================

def find_matching_bot_id(
    combined_text,
    log_rows,
    sender,
):

    bot_ids = extract_bot_ids(
        combined_text
    )

    if not bot_ids:
        return "", None

    # Сначала ищем точное соответствие в журнале.
    for raw_id in bot_ids:

        bot_id = raw_id.strip()

        send_log = find_send_log(
            log_rows,
            bot_id
        )

        if not send_log:
            continue

        if (
            send_log["email"]
            and send_log["email"] != sender
        ):
            continue

        return bot_id, send_log

    # Если есть ID, но карты нет.
    # Возвращаем первый ID для диагностического сообщения.
    return bot_ids[0].strip(), None


# ============================================================
# LEGACY v2.1
# ============================================================

def get_legacy_doctor_by_bot_id(
    bot_id,
    doctors_map
):

    match = re.match(
        r"^BOTON-(\d{8})-(.+)$",
        bot_id,
        flags=re.IGNORECASE
    )

    if not match:
        return None, None

    date_text = match.group(1)
    doctor_key = match.group(2).lower()

    try:

        target_date = datetime.strptime(
            date_text,
            "%Y%m%d"
        ).date()

    except ValueError:

        return None, None

    for doctor in doctors_map.values():

        key = make_doctor_key(
            doctor
        ).lower()

        if key == doctor_key:

            return doctor, target_date

    return None, target_date


# ============================================================
# ПОЛУЧЕНИЕ ПИСЕМ ОТ ВРАЧЕЙ
# ============================================================

def search_doctor_messages(
    mail,
    doctors_map,
):

    since_date = (
        today_moscow()
        - timedelta(
            days=IMAP_LOOKBACK_DAYS
        )
    )

    since_imap = since_date.strftime(
        "%d-%b-%Y"
    )

    print(
        f"IMAP: ищем письма врачей "
        f"с {since_date.strftime('%d.%m.%Y')}"
    )

    found = []
    seen_uids = set()

    doctor_emails = sorted({
        clean(
            doctor.get("email")
        ).lower()
        for doctor in doctors_map.values()
        if clean(
            doctor.get("email")
        )
    })

    for email in doctor_emails:

        if len(found) >= IMAP_MAX_TOTAL:
            break

        try:

            status, data = mail.search(
                None,
                "FROM",
                email,
                "SINCE",
                since_imap
            )

        except (
            imaplib.IMAP4.abort,
            EOFError,
            OSError,
        ) as error:

            print(
                f"IMAP: ошибка поиска "
                f"от {email}: {error}"
            )

            raise

        if status != "OK":
            continue

        ids = (
            data[0].split()
            if data and data[0]
            else []
        )

        # Берём самые новые.
        ids = ids[
            -IMAP_MAX_PER_DOCTOR:
        ]

        for uid in ids:

            uid_text = uid.decode(
                errors="replace"
            )

            if uid_text in seen_uids:
                continue

            seen_uids.add(uid_text)

            found.append({
                "uid": uid,
                "uid_text": uid_text,
                "sender_hint": email,
            })

            if len(found) >= IMAP_MAX_TOTAL:
                break

    return found


# ============================================================
# ВХОДЯЩИЕ ПИСЬМА
# ============================================================

def process_incoming_answers(
    worksheet,
    log_sheet,
    rows,
    doctors_map,
):

    print(
        "\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----"
    )

    try:

        mail = connect_imap()

    except Exception as error:

        print(
            f"IMAP ошибка подключения: "
            f"{type(error).__name__}: "
            f"{error}"
        )

        return {
            "processed": 0,
            "errors": 1,
        }

    processed = 0
    errors = 0

    try:

        # ----------------------------------------------------
        # Читаем журнал ОДИН раз.
        # ----------------------------------------------------

        log_rows = get_log_rows(
            log_sheet
        )

        # ----------------------------------------------------
        # Ищем только письма врачей.
        # ----------------------------------------------------

        print(
            "IMAP: поиск писем только от врачей..."
        )

        candidates = search_doctor_messages(
            mail,
            doctors_map
        )

        print(
            f"IMAP: найдено потенциальных писем: "
            f"{len(candidates)}"
        )

        for candidate in candidates:

            message_id = candidate[
                "uid_text"
            ]

            try:

                print(
                    f"IMAP: FETCH "
                    f"{message_id}..."
                )

                status, msg_data = mail.fetch(
                    candidate["uid"],
                    "(BODY.PEEK[])"
                )

                if status != "OK":

                    errors += 1

                    print(
                        f"IMAP: FETCH ошибка "
                        f"{message_id}"
                    )

                    continue

                raw_email = None

                for part in msg_data:

                    if (
                        isinstance(part, tuple)
                        and len(part) >= 2
                    ):

                        raw_email = part[1]
                        break

                if not raw_email:
                    continue

                message = BytesParser(
                    policy=policy.default
                ).parsebytes(
                    raw_email
                )

                sender = parseaddr(
                    message.get(
                        "From",
                        ""
                    )
                )[1].strip().lower()

                subject = decode_mime_header(
                    message.get(
                        "Subject",
                        ""
                    )
                )

                body = get_email_body(
                    message
                )

                # Настоящий Message-ID.
                real_message_id = clean(
                    message.get(
                        "Message-ID",
                        ""
                    )
                )

                # Если Message-ID отсутствует,
                # используем UID + sender.
                tracking_message_id = (
                    real_message_id
                    or f"imap:{sender}:{message_id}"
                )

                print(
                    f"IMAP: отправитель={sender}"
                )

                # ------------------------------------------------
                # Уже обработано?
                # ------------------------------------------------

                if message_was_processed(
                    log_rows,
                    tracking_message_id
                ):

                    print(
                        "IMAP: письмо уже обработано, "
                        "пропускаем."
                    )

                    continue

                # ------------------------------------------------
                # Врач должен быть в базе.
                # ------------------------------------------------

                doctor = None

                for db_doctor in doctors_map.values():

                    db_email = clean(
                        db_doctor.get("email")
                    ).lower()

                    if (
                        db_email
                        and db_email == sender
                    ):

                        doctor = db_doctor
                        break

                if not doctor:

                    print(
                        f"IMAP: отправитель не является "
                        f"врачом из базы: {sender}"
                    )

                    # ВАЖНО:
                    # ничего не STORE.
                    continue

                combined = (
                    subject
                    + "\n"
                    + body
                )

                bot_id, send_log = (
                    find_matching_bot_id(
                        combined,
                        log_rows,
                        sender
                    )
                )

                print(
                    f"IMAP: BOTON-ID="
                    f"{bot_id or 'не найден'}"
                )

                if not bot_id:

                    print(
                        "IMAP: письмо врача без "
                        "BOTON-ID — пропускаем."
                    )

                    continue

                # ------------------------------------------------
                # Точная v2.2 карта
                # ------------------------------------------------

                if send_log:

                    if (
                        send_log["email"]
                        and send_log["email"] != sender
                    ):

                        print(
                            "IMAP: email ответа не совпадает "
                            "с email отправления."
                        )

                        continue

                    mapping = send_log[
                        "mapping"
                    ]

                    if not mapping:

                        print(
                            "IMAP: в BoTon_Log отсутствует "
                            "карта задания."
                        )

                        continue

                    target_date = send_log[
                        "target_date"
                    ]

                    if not target_date:

                        print(
                            "IMAP: в журнале отсутствует дата."
                        )

                        continue

                    # ------------------------------------------------
                    # Обрабатываем ТОЛЬКО ответ врача.
                    # Цитату исходного письма исключаем.
                    # ------------------------------------------------

                    reply_text = extract_reply_text(
                        body
                    )

                    statistics, updates = (
                        apply_doctor_response(
                            worksheet,
                            mapping,
                            reply_text,
                        )
                    )

                    if updates:

                        processed += 1

                        result_text = (
                            f"processed; "
                            f"done={statistics['done']}; "
                            f"partial={statistics['partial']}; "
                            f"not_done={statistics['not_done']}"
                        )

                        append_log(
                            log_sheet,
                            event="RECEIVE",
                            bot_id=bot_id,
                            target_date=target_date,
                            doctor_name=doctor["name"],
                            email=sender,
                            message_id=tracking_message_id,
                            result=result_text,
                            notes="v2.2 exact mapping",
                        )

                        log_rows.append([
                            now_moscow().strftime(
                                "%d.%m.%Y %H:%M:%S"
                            ),
                            "RECEIVE",
                            bot_id,
                            target_date.strftime(
                                "%d.%m.%Y"
                            ),
                            doctor["name"],
                            sender,
                            "",
                            tracking_message_id,
                            result_text,
                            "v2.2 exact mapping",
                        ])

                        print(
                            f"Ответ обработан: "
                            f"{doctor['name']} "
                            f"| BOTON-ID={bot_id} "
                            f"| выполнено="
                            f"{statistics['done']} "
                            f"| частично="
                            f"{statistics['partial']} "
                            f"| не выполнено="
                            f"{statistics['not_done']}"
                        )

                    else:

                        print(
                            f"Ответ от "
                            f"{doctor['name']} "
                            f"не содержит "
                            f"распознаваемых отметок."
                        )

                        append_log(
                            log_sheet,
                            event="RECEIVE",
                            bot_id=bot_id,
                            target_date=target_date,
                            doctor_name=doctor["name"],
                            email=sender,
                            message_id=tracking_message_id,
                            result="NO_STATUS",
                            notes="Нет распознаваемых статусов",
                        )

                        log_rows.append([
                            now_moscow().strftime(
                                "%d.%m.%Y %H:%M:%S"
                            ),
                            "RECEIVE",
                            bot_id,
                            target_date.strftime(
                                "%d.%m.%Y"
                            ),
                            doctor["name"],
                            sender,
                            "",
                            tracking_message_id,
                            "NO_STATUS",
                            "Нет распознаваемых статусов",
                        ])

                    continue

                # ------------------------------------------------
                # LEGACY v2.1
                #
                # Старый BOTON-ID не имеет точной карты.
                # Обрабатываем только как совместимость.
                # Новые письма сюда не попадут.
                # ------------------------------------------------

                legacy_doctor, legacy_date = (
                    get_legacy_doctor_by_bot_id(
                        bot_id,
                        doctors_map
                    )
                )

                if not legacy_doctor:

                    print(
                        f"IMAP: BOTON-ID {bot_id} "
                        f"не найден в BoTon_Log."
                    )

                    continue

                print(
                    f"IMAP: LEGACY BOTON-ID "
                    f"{bot_id}; точной карты нет."
                )

                legacy_patients = (
                    get_patients_for_doctor(
                        rows,
                        legacy_doctor,
                        legacy_date,
                        include_processed=True,
                    )
                )

                if not legacy_patients:

                    print(
                        "IMAP: legacy-задания "
                        "не найдены."
                    )

                    continue

                legacy_mapping = (
                    build_exact_response_task_map(
                        legacy_patients
                    )
                )

                reply_text = extract_reply_text(
                    body
                )

                statistics, updates = (
                    apply_doctor_response(
                        worksheet,
                        legacy_mapping,
                        reply_text,
                    )
                )

                if updates:

                    processed += 1

                    result_text = (
                        f"legacy_processed; "
                        f"done={statistics['done']}; "
                        f"partial={statistics['partial']}; "
                        f"not_done={statistics['not_done']}"
                    )

                    append_log(
                        log_sheet,
                        event="RECEIVE",
                        bot_id=bot_id,
                        target_date=legacy_date,
                        doctor_name=legacy_doctor["name"],
                        email=sender,
                        message_id=tracking_message_id,
                        result=result_text,
                        notes=(
                            "LEGACY v2.1: "
                            "карта восстановлена "
                            "по текущей таблице"
                        ),
                    )

                    log_rows.append([
                        now_moscow().strftime(
                            "%d.%m.%Y %H:%M:%S"
                        ),
                        "RECEIVE",
                        bot_id,
                        legacy_date.strftime(
                            "%d.%m.%Y"
                        ),
                        legacy_doctor["name"],
                        sender,
                        "",
                        tracking_message_id,
                        result_text,
                        "LEGACY v2.1",
                    ])

                    print(
                        f"Legacy-ответ обработан: "
                        f"{legacy_doctor['name']} "
                        f"| выполнено="
                        f"{statistics['done']} "
                        f"| частично="
                        f"{statistics['partial']} "
                        f"| не выполнено="
                        f"{statistics['not_done']}"
                    )

                else:

                    print(
                        "Legacy-ответ не содержит "
                        "распознаваемых статусов."
                    )

            except (
                imaplib.IMAP4.abort,
                EOFError,
                OSError,
            ) as error:

                errors += 1

                print(
                    "IMAP: соединение прервано "
                    f"во время обработки: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                print(
                    "IMAP: прекращаю текущую "
                    "обработку входящих."
                )

                break

            except Exception as error:

                errors += 1

                print(
                    f"Ошибка обработки письма: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                # НИКАКОГО STORE.
                continue

    except (
        imaplib.IMAP4.abort,
        EOFError,
        OSError,
    ) as error:

        errors += 1

        print(
            f"IMAP: соединение аварийно "
            f"завершено: {error}"
        )

    finally:

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

    return {
        "processed": processed,
        "errors": errors,
    }


# ============================================================
# СТАТИСТИКА
# ============================================================

def calculate_report(
    rows,
    doctors_map,
    target_date,
):

    doctor_stats = {}

    total_patients = 0
    total_tasks = 0

    total_done = 0
    total_partial = 0
    total_not_done = 0

    answered_doctors = set()

    for doctor in doctors_map.values():

        patients = get_patients_for_doctor(
            rows,
            doctor,
            target_date,
            include_processed=True,
        )

        if not patients:
            continue

        doctor_key = get_signature(
            doctor["name"]
        )

        stat = {
            "doctor": doctor["name"],
            "patients": len(patients),
            "tasks": 0,
            "done": 0,
            "partial": 0,
            "not_done": 0,
        }

        for patient in patients:

            stat["tasks"] += len(
                patient["tasks"]
            )

        doctor_stats[
            doctor_key
        ] = stat

        total_patients += len(
            patients
        )

        total_tasks += stat["tasks"]

    for row in rows:

        row_date = parse_date(
            row[COL_DATE]
            if len(row) > COL_DATE
            else ""
        )

        if row_date != target_date:
            continue

        branch = (
            row[COL_BRANCH]
            if len(row) > COL_BRANCH
            else ""
        )

        if not is_target_branch(branch):
            continue

        doctor_name = clean(
            row[COL_DOCTOR]
            if len(row) > COL_DOCTOR
            else ""
        )

        doctor = find_doctor(
            doctor_name,
            doctors_map
        )

        if not doctor:
            continue

        doctor_key = get_signature(
            doctor["name"]
        )

        if doctor_key not in doctor_stats:
            continue

        statuses = [
            clean(
                row[COL_DONE1]
                if len(row) > COL_DONE1
                else ""
            ),
            clean(
                row[COL_DONE2]
                if len(row) > COL_DONE2
                else ""
            ),
            clean(
                row[COL_DONE3]
                if len(row) > COL_DONE3
                else ""
            ),
        ]

        for status in statuses:

            if not status:
                continue

            if status.startswith("✅"):

                doctor_stats[
                    doctor_key
                ]["done"] += 1

                total_done += 1

                answered_doctors.add(
                    doctor_key
                )

            elif status.startswith("⚠️"):

                doctor_stats[
                    doctor_key
                ]["partial"] += 1

                total_partial += 1

                answered_doctors.add(
                    doctor_key
                )

            elif status.startswith("❌"):

                doctor_stats[
                    doctor_key
                ]["not_done"] += 1

                total_not_done += 1

                answered_doctors.add(
                    doctor_key
                )

    return {
        "doctors": doctor_stats,
        "patients": total_patients,
        "tasks": total_tasks,
        "done": total_done,
        "partial": total_partial,
        "not_done": total_not_done,
        "answered_doctors": len(
            answered_doctors
        ),
    }


# ============================================================
# ОТЧЁТ
# ============================================================

def build_report(
    report,
    target_date,
):

    lines = []

    lines.append(
        "BoTon — отчёт по выполнению заданий"
    )

    lines.append(
        target_date.strftime(
            "%d.%m.%Y"
        )
    )

    lines.append("")

    doctors = report["doctors"]

    lines.append(
        f"Врачей: {len(doctors)}"
    )

    lines.append(
        f"Пациентов: {report['patients']}"
    )

    lines.append(
        f"Заданий: {report['tasks']}"
    )

    lines.append("")

    lines.append(
        f"Ответили: "
        f"{report['answered_doctors']}"
    )

    lines.append(
        f"Не ответили: "
        f"{max(0, len(doctors) - report['answered_doctors'])}"
    )

    lines.append("")

    lines.append(
        f"Выполнено: "
        f"{report['done']}"
    )

    lines.append(
        f"Частично: "
        f"{report['partial']}"
    )

    lines.append(
        f"Не выполнено: "
        f"{report['not_done']}"
    )

    lines.append("")

    lines.append(
        "По врачам:"
    )

    for stat in sorted(
        doctors.values(),
        key=lambda x: x["doctor"]
    ):

        lines.append("")

        lines.append(
            stat["doctor"]
        )

        lines.append(
            f"{stat['patients']} пациентов / "
            f"{stat['tasks']} заданий"
        )

        lines.append(
            f"✅ Выполнено: "
            f"{stat['done']}"
        )

        lines.append(
            f"⚠️ Частично: "
            f"{stat['partial']}"
        )

        lines.append(
            f"❌ Не выполнено: "
            f"{stat['not_done']}"
        )

    lines.append("")

    lines.append(
        "Отчёт сформирован автоматически."
    )

    lines.append(
        "BoTon"
    )

    return "\n".join(lines)


def send_report(report_text):

    subject = (
        f"BoTon — отчёт по заданиям "
        f"за {today_str()}"
    )

    smtp = None

    try:

        smtp = smtp_connect()

        send_email(
            smtp,
            RESERVE_EMAIL,
            subject,
            report_text,
        )

        print(
            f"Отчёт отправлен на "
            f"{RESERVE_EMAIL}"
        )

    finally:

        if smtp:

            try:
                smtp.quit()

            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        f"\nBOTON v{VERSION}"
    )

    print(
        f"TEST_MODE = {TEST_MODE}"
    )

    print(
        f"Дата: {today_str()}"
    )

    # --------------------------------------------------------
    # Google Sheets
    # --------------------------------------------------------

    print(
        "Подключение к Google Sheets..."
    )

    (
        tasks_ss,
        tasks_sheet,
        doctors_sheet,
        log_sheet,
    ) = get_sheets()

    print(
        "Google Sheets: OK"
    )

    rows = tasks_sheet.get_all_values()

    print(
        f"Прочитано строк: "
        f"{max(0, len(rows) - 1)}"
    )

    doctors_map = get_doctors_map(
        doctors_sheet
    )

    print(
        f"Врачей в базе: "
        f"{len(doctors_map)}"
    )

    print(
        f"BoTon_Log: {LOG_SHEET_NAME}"
    )

    target_date = today_moscow()

    # --------------------------------------------------------
    # Входящие ответы
    # --------------------------------------------------------

    incoming = process_incoming_answers(
        tasks_sheet,
        log_sheet,
        rows,
        doctors_map,
    )

    print(
        f"Обработано входящих ответов: "
        f"{incoming['processed']}"
    )

    if incoming["errors"]:

        print(
            f"Ошибок входящих писем: "
            f"{incoming['errors']}"
        )

    # --------------------------------------------------------
    # Рассылка
    # --------------------------------------------------------

    if not TEST_RECEIVE_ONLY:

        print(
            "\n----- РАССЫЛКА -----"
        )

        send_today_assignments(
            tasks_sheet,
            log_sheet,
            doctors_map,
            rows,
            target_date,
        )

    # --------------------------------------------------------
    # Итог
    # --------------------------------------------------------

    print(
        "\n----- ИТОГОВАЯ СТАТИСТИКА -----"
    )

    rows_after = (
        tasks_sheet.get_all_values()
    )

    report = calculate_report(
        rows_after,
        doctors_map,
        target_date,
    )

    print(
        f"Врачей: "
        f"{len(report['doctors'])}"
    )

    print(
        f"Пациентов: "
        f"{report['patients']}"
    )

    print(
        f"Заданий: "
        f"{report['tasks']}"
    )

    print(
        f"Выполнено: "
        f"{report['done']}"
    )

    print(
        f"Частично: "
        f"{report['partial']}"
    )

    print(
        f"Не выполнено: "
        f"{report['not_done']}"
    )

    print(
        "\nBOTON завершён."
    )


if __name__ == "__main__":
    main()
