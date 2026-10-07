# ============================================================
# BoTon v2.0
# Рассылка заданий + приём ответов врачей + запись H/J/L
# + отчёт заведующему
#
# VERSION: 2.0
# ============================================================

import os
import re
import ssl
import imaplib
import smtplib
from collections import defaultdict
from datetime import datetime, date
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

import gspread
from google.oauth2.service_account import Credentials
from zoneinfo import ZoneInfo


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

VERSION = "2.0"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ------------------------------------------------------------
# Google Sheets
# ------------------------------------------------------------

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

# ------------------------------------------------------------
# Колонки базы врачей
# A = ФИО
# B = Chat ID
# C = Email
# ------------------------------------------------------------

COL_DOCTOR_NAME = 0
COL_DOCTOR_CHAT_ID = 1
COL_DOCTOR_EMAIL = 2

# ------------------------------------------------------------
# Колонки таблицы заданий
# ------------------------------------------------------------

COL_DATE = 0       # A
COL_BRANCH = 1     # B
COL_DOCTOR = 2     # C
COL_OMS = 3        # D
COL_PATIENT = 4    # E
COL_SOURCE = 5     # F

COL_TASK1 = 6      # G
COL_DONE1 = 7      # H

COL_TASK2 = 8      # I
COL_DONE2 = 9      # J

COL_TASK3 = 10     # K
COL_DONE3 = 11     # L

COL_SEND_STATUS = 12  # M


# ============================================================
# ENV / SECRETS
# ============================================================

GOOGLE_SERVICE_ACCOUNT = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USER)

# IMAP.
# Для Яндекса:
# IMAP_HOST = imap.yandex.ru
# IMAP_PORT = 993
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", SMTP_USER)
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", SMTP_PASSWORD)

# Резервная почта
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
# Тестовый режим
# ------------------------------------------------------------

TEST_MODE = False

# Если True — письма не отправляются и таблица не меняется.
# Удобно для первого запуска v2.0.
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

    if value.lower() in {"null", "undefined"}:
        return ""

    return value


def normalize_text(value):
    value = clean(value).lower()
    value = value.replace("ё", "е")
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
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
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


# ============================================================
# ФИО ВРАЧА
# ============================================================

def get_signature(name):
    """
    Преобразует:

    О.В. Нецветаева
    Нецветаева О.В.
    Нецветаева Ольга Викторовна

    в сопоставимый ключ.
    """

    name = normalize_text(name)

    if not name:
        return ""

    parts = name.split()

    if not parts:
        return ""

    # Если первая часть — инициалы, а фамилия идёт второй
    if len(parts) >= 2 and re.fullmatch(r"[а-я]\.?[а-я]?\.?", parts[0]):
        surname = parts[1]
        initials = re.sub(r"[^а-я]", "", parts[0])

        return f"{surname} {initials}".strip()

    surname = parts[0]

    initials = ""

    for part in parts[1:]:
        letters = re.sub(r"[^а-я]", "", part)

        if letters:
            initials += letters[0]

    return f"{surname} {initials}".strip()


def surname_of_signature(signature):
    return signature.split()[0] if signature else ""


def initials_of_signature(signature):
    parts = signature.split()

    return parts[1] if len(parts) > 1 else ""


def find_doctor(contact_name, doctors_map):
    """
    Надёжное сопоставление врача:
    1. exact
    2. фамилия + инициалы
    3. совместимые инициалы
    4. частичное совпадение фамилии
    """

    signature = get_signature(contact_name)

    if not signature:
        return None

    if signature in doctors_map:
        return doctors_map[signature]

    surname = surname_of_signature(signature)
    initials = initials_of_signature(signature)

    # Фамилия + совместимые инициалы
    for key, doctor in doctors_map.items():

        db_surname = surname_of_signature(key)
        db_initials = initials_of_signature(key)

        if db_surname != surname:
            continue

        if not initials or not db_initials:
            return doctor

        if (
            initials.startswith(db_initials)
            or db_initials.startswith(initials)
        ):
            return doctor

    # Частичное совпадение фамилии
    if len(surname) >= 4:

        for key, doctor in doctors_map.items():

            db_surname = surname_of_signature(key)

            if (
                surname in db_surname
                or db_surname in surname
            ):
                return doctor

    return None


# ============================================================
# ПОЛУЧЕНИЕ GOOGLE SHEETS
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

    credentials_info = __import__("json").loads(
        GOOGLE_SERVICE_ACCOUNT
    )

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=scopes,
    )

    return gspread.authorize(credentials)


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

    return tasks_sheet, doctors_sheet


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

        signature = get_signature(name)

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


def is_status_empty(value):
    value = clean(value)

    return value == ""


def make_send_status(email):
    now = now_moscow()

    return (
        f"✅ Отправлено "
        f"{now.strftime('%d.%m.%Y %H:%M')} "
        f"на {email}"
    )


def make_reserve_status():
    now = now_moscow()

    return (
        f"✅ Отправлено "
        f"{now.strftime('%d.%m.%Y %H:%M')} "
        f"на резервную почту"
    )


def make_error_status():
    now = now_moscow()

    return (
        f"❌ Ошибка отправки "
        f"{now.strftime('%d.%m.%Y %H:%M')}"
    )


def get_tasks_from_row(row, row_number):

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
# ГРУППИРОВКА ПАЦИЕНТОВ
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

    # Основной ключ — ID/ОМС.
    # Если его нет — ФИО.
    return oms or normalize_text(patient)


def build_doctor_patients(rows, doctor_name, target_date):

    patients = {}

    for index, row in enumerate(rows, start=2):

        if len(row) <= COL_DOCTOR:
            continue

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

        row_doctor = clean(
            row[COL_DOCTOR]
        )

        if not row_doctor:
            continue

        if not find_doctor(
            row_doctor,
            {
                get_signature(doctor_name): {
                    "name": doctor_name
                }
            },
        ):
            continue

        status = clean(
            row[COL_SEND_STATUS]
            if len(row) > COL_SEND_STATUS
            else ""
        )

        # M заполнена = строка уже отправлялась.
        if status:
            continue

        tasks = get_tasks_from_row(
            row,
            index
        )

        if not tasks:
            continue

        key = patient_key(row)

        if key not in patients:

            patients[key] = {
                "patient": clean(row[COL_PATIENT]),
                "oms": clean(row[COL_OMS]),
                "rows": [],
                "tasks": [],
            }

        patients[key]["rows"].append(index)
        patients[key]["tasks"].extend(tasks)

    return list(patients.values())


# ============================================================
# ПОЛУЧЕНИЕ ВСЕХ ЗАДАНИЙ НА СЕГОДНЯ
# ============================================================

def collect_today_assignments(
    rows,
    doctors_map,
    target_date,
):

    assignments = defaultdict(list)

    diagnostics = {
        "empty": 0,
        "bad_date": 0,
        "other_date": 0,
        "other_branch": 0,
        "processed": 0,
        "without_doctor": 0,
        "doctor_not_found": 0,
        "without_tasks": 0,
    }

    for index, row in enumerate(rows, start=2):

        if not row:
            diagnostics["empty"] += 1
            continue

        row_date = parse_date(
            row[COL_DATE]
            if len(row) > COL_DATE
            else ""
        )

        if row_date is None:
            diagnostics["bad_date"] += 1
            continue

        if row_date != target_date:
            diagnostics["other_date"] += 1
            continue

        branch = (
            row[COL_BRANCH]
            if len(row) > COL_BRANCH
            else ""
        )

        if not is_target_branch(branch):
            diagnostics["other_branch"] += 1
            continue

        doctor_name = clean(
            row[COL_DOCTOR]
            if len(row) > COL_DOCTOR
            else ""
        )

        if not doctor_name:
            diagnostics["without_doctor"] += 1
            continue

        doctor = find_doctor(
            doctor_name,
            doctors_map
        )

        if not doctor:
            diagnostics["doctor_not_found"] += 1
            continue

        status = clean(
            row[COL_SEND_STATUS]
            if len(row) > COL_SEND_STATUS
            else ""
        )

        if status:
            continue

        tasks = get_tasks_from_row(
            row,
            index
        )

        if not tasks:
            diagnostics["without_tasks"] += 1
            continue

        doctor_key = get_signature(
            doctor["name"]
        )

        assignments[doctor_key].append({
            "row_number": index,
            "row": row,
            "doctor": doctor,
            "tasks": tasks,
        })

        diagnostics["processed"] += 1

    return assignments, diagnostics


# ============================================================
# ПЕРЕГРУППИРОВКА ПО ПАЦИЕНТАМ
# ============================================================

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

    return list(patients.values())


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


def task_label(patient_number, task_index, total_tasks):

    # Если у пациента одно задание — только номер.
    if total_tasks == 1:
        return str(patient_number)

    return (
        f"{patient_number}"
        f"{TASK_LETTERS[task_index]}"
    )


# ============================================================
# ТЕКСТ ПИСЬМА
# ============================================================

def extract_first_name_patronymic(full_name):

    """
    База A обычно содержит:
    Вельмакина Ю.А.

    Поэтому без отдельного справочника
    нельзя безопасно восстановить полное имя.

    Если в будущем в базе появятся полные ФИО,
    функция автоматически сможет их использовать.
    """

    name = clean(full_name)

    # Полное ФИО
    parts = name.split()

    if len(parts) >= 3:

        first = parts[1]
        patronymic = parts[2]

        return f"{first} {patronymic}"

    # Пока безопасный fallback
    return name


def make_greeting(doctor):

    display_name = doctor.get(
        "full_name"
    ) or extract_first_name_patronymic(
        doctor["name"]
    )

    return (
        f"Уважаемая(ый) {display_name}!"
    )


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

        for task_index, task in enumerate(tasks):

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
        "Ответьте на это письмо, указав выполненные "
        "и невыполненные задания."
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

    # Технический ID.
    # Нужен боту для определения конкретной рассылки.
    lines.append("")
    lines.append(
        f"[BOTON-ID:{bot_id}]"
    )

    return "\n".join(lines)


# ============================================================
# ИДЕНТИФИКАТОР РАССЫЛКИ
# ============================================================

def make_doctor_key(doctor):

    signature = get_signature(
        doctor["name"]
    )

    return re.sub(
        r"[^a-zа-я0-9]+",
        "-",
        signature
    )


def make_bot_id(target_date, doctor):

    date_part = target_date.strftime(
        "%Y%m%d"
    )

    doctor_key = make_doctor_key(
        doctor
    )

    return (
        f"BOTON-{date_part}-"
        f"{doctor_key}"
    )


# ============================================================
# SMTP
# ============================================================

def smtp_connect():

    context = ssl.create_default_context()

    server = smtplib.SMTP_SSL(
        SMTP_HOST,
        SMTP_PORT,
        context=context,
        timeout=30,
    )

    server.login(
        SMTP_USER,
        SMTP_PASSWORD,
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
            result.append(str(part))

    return "".join(result)


def get_email_body(message):

    if message.is_multipart():

        plain_parts = []

        for part in message.walk():

            content_type = part.get_content_type()

            disposition = str(
                part.get("Content-Disposition") or ""
            )

            if (
                content_type == "text/plain"
                and "attachment" not in disposition.lower()
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


def extract_bot_id(text):

    match = re.search(
        r"\[BOTON-ID:([^\]]+)\]",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1).strip()

    return ""


def connect_imap():

    mail = imaplib.IMAP4_SSL(
        IMAP_HOST,
        IMAP_PORT
    )

    mail.login(
        IMAP_USER,
        IMAP_PASSWORD
    )

    mail.select("INBOX")

    return mail


# ============================================================
# РАЗБОР ОТВЕТА ВРАЧА
# ============================================================

STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_NOT_DONE = "not_done"


def normalize_answer_text(text):

    text = text.replace("\r", "\n")

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    return text


def parse_status_sections(text):

    """
    Ищет:

    Выполнено: 1, 2А, 3
    Не выполнено: 2Б
    Частично: 3В

    Допускаются:
    Выполнено - 1, 2
    Выполнено 1, 2
    """

    text = normalize_answer_text(
        text
    )

    sections = {
        STATUS_DONE: [],
        STATUS_PARTIAL: [],
        STATUS_NOT_DONE: [],
    }

    patterns = {
        STATUS_DONE: [
            r"выполнено\s*[:\-]?\s*([^\n]+)",
            r"выполнены\s*[:\-]?\s*([^\n]+)",
        ],

        STATUS_PARTIAL: [
            r"частично\s*[:\-]?\s*([^\n]+)",
        ],

        STATUS_NOT_DONE: [
            r"не\s+выполнено\s*[:\-]?\s*([^\n]+)",
            r"не\s+выполнены\s*[:\-]?\s*([^\n]+)",
        ],
    }

    for status, status_patterns in patterns.items():

        for pattern in status_patterns:

            matches = re.findall(
                pattern,
                text,
                flags=re.IGNORECASE
            )

            for match in matches:

                tokens = re.findall(
                    r"\b(\d+)\s*([А-ЯЁа-яё]?)\b",
                    match
                )

                for number, letter in tokens:

                    number = int(number)

                    letter = (
                        letter.upper()
                        if letter
                        else ""
                    )

                    sections[status].append(
                        (number, letter)
                    )

    return sections


# ============================================================
# СОЗДАНИЕ КАРТЫ ЗАДАНИЙ ДЛЯ ОТВЕТА
# ============================================================

def build_response_task_map(
    patients
):

    mapping = {}

    for patient_number, patient in enumerate(
        patients,
        start=1
    ):

        tasks = patient["tasks"]

        for task_index, task in enumerate(tasks):

            if len(tasks) == 1:

                key = (
                    patient_number,
                    ""
                )

            else:

                key = (
                    patient_number,
                    TASK_LETTERS[task_index]
                )

            mapping[key] = task

    return mapping


# ============================================================
# РАЗВОРАЧИВАНИЕ ОТМЕТОК
# ============================================================

def expand_status_items(
    items,
    patients,
    mapping,
):

    result = set()

    for patient_number, letter in items:

        # Номер без буквы = ВСЕ задания пациента.
        if not letter:

            if (
                patient_number < 1
                or patient_number > len(patients)
            ):
                continue

            patient = patients[
                patient_number - 1
            ]

            for task_index, task in enumerate(
                patient["tasks"]
            ):

                if len(patient["tasks"]) == 1:

                    key = (
                        patient_number,
                        ""
                    )

                else:

                    key = (
                        patient_number,
                        TASK_LETTERS[task_index]
                    )

                if key in mapping:
                    result.add(key)

        else:

            key = (
                patient_number,
                letter
            )

            if key in mapping:
                result.add(key)

    return result


# ============================================================
# ПРИМЕНЕНИЕ ОТВЕТА
# ============================================================

def apply_doctor_response(
    worksheet,
    patients,
    answer_text,
):

    sections = parse_status_sections(
        answer_text
    )

    mapping = build_response_task_map(
        patients
    )

    done = expand_status_items(
        sections[STATUS_DONE],
        patients,
        mapping,
    )

    partial = expand_status_items(
        sections[STATUS_PARTIAL],
        patients,
        mapping,
    )

    not_done = expand_status_items(
        sections[STATUS_NOT_DONE],
        patients,
        mapping,
    )

    # Приоритет:
    # Не выполнено > Частично > Выполнено
    #
    # Это защищает от случайного пересечения
    # одного задания в разных разделах.

    partial -= not_done
    done -= not_done
    done -= partial

    updates = []

    timestamp = now_moscow().strftime(
        "%d.%m.%Y %H:%M"
    )

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

    for key, task in mapping.items():

        if key in done:
            status = STATUS_DONE

        elif key in partial:
            status = STATUS_PARTIAL

        elif key in not_done:
            status = STATUS_NOT_DONE

        else:
            continue

        row_number = task[
            "row_number"
        ]

        status_col = task[
            "status_col"
        ]

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
# ПОИСК ПАЦИЕНТОВ ПО ДНЮ И ВРАЧУ
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
            if len(row) > COL_DOCTOR
            else ""
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

        if send_status and not include_processed:
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
# ОТПРАВКА УТРЕННИХ ЗАДАНИЙ
# ============================================================

def send_today_assignments(
    worksheet,
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

        print("SMTP: OK")

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

            bot_id = make_bot_id(
                target_date,
                doctor
            )

            subject = (
                f"BoTon — задания "
                f"на {target_date.strftime('%d.%m.%Y')} "
                f"[{bot_id}]"
            )

            body = build_email_body(
                doctor,
                patients,
                target_date,
                bot_id,
            )

            if TEST_MODE or TEST_RECEIVE_ONLY:

                print(
                    f"\n===== TEST EMAIL =====\n"
                    f"To: {email}\n"
                    f"Subject: {subject}\n\n"
                    f"{body}\n"
                    f"======================\n"
                )

                continue

            try:

                send_email(
                    smtp,
                    email,
                    subject,
                    body,
                )

                updates = []

                status = make_send_status(
                    email
                )

                for item in items:

                    cell = gspread.utils.rowcol_to_a1(
                        item["row_number"],
                        COL_SEND_STATUS + 1
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

                print(
                    f"Письмо врачу отправлено: "
                    f"{doctor['name']} "
                    f"({email})"
                )

            except Exception as error:

                print(
                    f"Ошибка отправки "
                    f"{doctor['name']}: "
                    f"{error}"
                )

                if not TEST_MODE:

                    updates = []

                    status = make_error_status()

                    for item in items:

                        cell = gspread.utils.rowcol_to_a1(
                            item["row_number"],
                            COL_SEND_STATUS + 1
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
# ОБРАБОТКА ВХОДЯЩИХ ОТВЕТОВ
# ============================================================

def process_incoming_answers(
    worksheet,
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
            f"IMAP ошибка подключения: {error}"
        )

        return {
            "processed": 0,
            "errors": 0,
        }

    processed = 0
    errors = 0

    try:

        status, data = mail.search(
            None,
            "UNSEEN"
        )

        if status != "OK":

            print(
                "Не удалось получить список "
                "непрочитанных писем."
            )

            return {
                "processed": 0,
                "errors": 0,
            }

        message_ids = data[0].split()

        print(
            f"Непрочитанных писем: "
            f"{len(message_ids)}"
        )

        for message_id in message_ids:

            try:

                status, msg_data = mail.fetch(
                    message_id,
                    "(RFC822)"
                )

                if status != "OK":
                    continue

                raw_email = msg_data[0][1]

                message = BytesParser(
                    policy=policy.default
                ).parsebytes(
                    raw_email
                )

                sender = parseaddr(
                    message.get("From", "")
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

                combined = (
                    subject
                    + "\n"
                    + body
                )

                bot_id = extract_bot_id(
                    combined
                )

                if not bot_id:
                    # Не наше письмо.
                    # Помечаем прочитанным, чтобы
                    # не обрабатывать снова.
                    mail.store(
                        message_id,
                        "+FLAGS",
                        "\\Seen"
                    )
                    continue

                # ------------------------------------------------
                # Врач определяется по email отправителя.
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
                        f"Ответ от неизвестного email: "
                        f"{sender}"
                    )

                    mail.store(
                        message_id,
                        "+FLAGS",
                        "\\Seen"
                    )

                    continue

                # ------------------------------------------------
                # Из BOTON-ID получаем дату.
                # BOTON-20261008-...
                # ------------------------------------------------

                date_match = re.search(
                    r"BOTON-(\d{8})-",
                    bot_id,
                    flags=re.IGNORECASE
                )

                if not date_match:

                    print(
                        f"Неверный BOTON-ID: "
                        f"{bot_id}"
                    )

                    mail.store(
                        message_id,
                        "+FLAGS",
                        "\\Seen"
                    )

                    continue

                target_date = datetime.strptime(
                    date_match.group(1),
                    "%Y%m%d"
                ).date()

                # ------------------------------------------------
                # Восстанавливаем пациентов и задания
                # именно для этой даты и этого врача.
                # include_processed=True важно:
                # M уже заполнена после рассылки.
                # ------------------------------------------------

                patients = get_patients_for_doctor(
                    rows,
                    doctor,
                    target_date,
                    include_processed=True,
                )

                if not patients:

                    print(
                        f"Не найдены задания для "
                        f"{doctor['name']} "
                        f"на {target_date}"
                    )

                    mail.store(
                        message_id,
                        "+FLAGS",
                        "\\Seen"
                    )

                    continue

                statistics, updates = (
                    apply_doctor_response(
                        worksheet,
                        patients,
                        body,
                    )
                )

                if updates:

                    processed += 1

                    print(
                        f"Ответ обработан: "
                        f"{doctor['name']} "
                        f"| выполнено={statistics['done']} "
                        f"| частично={statistics['partial']} "
                        f"| не выполнено={statistics['not_done']}"
                    )

                else:

                    print(
                        f"Ответ от {doctor['name']} "
                        f"не содержит распознаваемых отметок."
                    )

                # Помечаем письмо обработанным.
                mail.store(
                    message_id,
                    "+FLAGS",
                    "\\Seen"
                )

            except Exception as error:

                errors += 1

                print(
                    f"Ошибка обработки письма: "
                    f"{error}"
                )

                # Чтобы одно битое письмо
                # не блокировало всю очередь.
                try:
                    mail.store(
                        message_id,
                        "+FLAGS",
                        "\\Seen"
                    )
                except Exception:
                    pass

    finally:

        try:
            mail.close()
        except Exception:
            pass

        try:
            mail.logout()
        except Exception:
            pass

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

    # Пациенты группируем отдельно по врачу.
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
            "no_answer": 0,
        }

        for patient in patients:

            stat["tasks"] += len(
                patient["tasks"]
            )

            for task in patient["tasks"]:

                status = ""

                # Статус находится в конкретной
                # ячейке H/J/L. Из rows его здесь
                # не восстанавливаем напрямую.
                #
                # Поэтому статистика ниже строится
                # по содержимому status cells,
                # которое передаётся отдельным проходом.
                _ = task

        doctor_stats[doctor_key] = stat

        total_patients += len(patients)
        total_tasks += stat["tasks"]

    # Второй проход по таблице для подсчёта H/J/L.
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
# ОТЧЁТ ЗАВЕДУЮЩЕМУ
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
        target_date.strftime("%d.%m.%Y")
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
        f"Выполнено: {report['done']}"
    )

    lines.append(
        f"Частично: {report['partial']}"
    )

    lines.append(
        f"Не выполнено: {report['not_done']}"
    )

    lines.append("")

    lines.append("По врачам:")

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
            f"✅ Выполнено: {stat['done']}"
        )

        lines.append(
            f"⚠️ Частично: {stat['partial']}"
        )

        lines.append(
            f"❌ Не выполнено: {stat['not_done']}"
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
# ОСНОВНОЙ ЗАПУСК
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

    tasks_sheet, doctors_sheet = get_sheets()

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

    target_date = today_moscow()

    # --------------------------------------------------------
    # Сначала принимаем ответы.
    # Это важно: если врач ответил между
    # запусками GitHub Actions, ответ будет
    # обработан до следующей рассылки.
    # --------------------------------------------------------

    incoming = process_incoming_answers(
        tasks_sheet,
        rows,
        doctors_map,
    )

    print(
        f"Обработано входящих ответов: "
        f"{incoming['processed']}"
    )

    # --------------------------------------------------------
    # Затем отправляем новые задания.
    # --------------------------------------------------------

    if not TEST_RECEIVE_ONLY:

        print(
            "\n----- РАССЫЛКА -----"
        )

        send_today_assignments(
            tasks_sheet,
            doctors_map,
            rows,
            target_date,
        )

    # --------------------------------------------------------
    # Итоговая диагностика
    # --------------------------------------------------------

    print(
        "\n----- ИТОГОВАЯ СТАТИСТИКА -----"
    )

    rows_after = tasks_sheet.get_all_values()

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
