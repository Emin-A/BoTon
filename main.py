import os
import re
import json
import html
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# BOTON v1.6
# ============================================================

VERSION = "1.6"

TEST_MODE = False

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

TASKS_SHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_SHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

RESERVE_EMAIL = "eagaverdiev@mosmedzdrav.ru"


# ============================================================
# КОЛОНКИ ТАБЛИЦЫ ЗАДАНИЙ
# ============================================================

COL_DATE = 0
COL_BRANCH = 1
COL_DOCTOR = 2
COL_PATIENT_ID = 3
COL_PATIENT_NAME = 4
COL_SOURCE = 5

COL_TASK1 = 6       # G
COL_TASK2 = 8       # I
COL_TASK3 = 10      # K

COL_COMPLETION = 12 # M


# ============================================================
# КОЛОНКИ БАЗЫ ВРАЧЕЙ
# A = ФИО
# B = Chat ID
# C = Email
# ============================================================

COL_DOCTOR_EMAIL = 2


# ============================================================
# ФИЛИАЛ №2
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
# НАЗВАНИЯ ЗАДАНИЙ
# ============================================================

TASK_NAMES = {
    COL_TASK1: "Задание 1 (липиды)",
    COL_TASK2: "Задание 2 (КДП — выгрузка)",
    COL_TASK3: "Задание 3 (КДП СД 3 квартал)",
}


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_text(value):
    """Нормализация текста для сравнения."""

    if value is None:
        return ""

    value = str(value).strip().lower()
    value = value.replace("ё", "е")

    value = re.sub(r"[№.,;:()\-_/]+", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_email(value):
    if not value:
        return ""

    return str(value).strip().lower()


def is_branch_2(branch):
    """Проверка принадлежности к филиалу №2."""

    branch = normalize_text(branch)

    if not branch:
        return False

    return any(
        keyword in branch
        for keyword in BRANCH_KEYWORDS
    )


def is_empty_completion(value):
    """
    Необработанная строка:
    пусто / null / undefined.
    """

    value = normalize_text(value)

    return value in (
        "",
        "null",
        "undefined",
    )


def parse_date(value):
    """Преобразование даты из таблицы."""

    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d",
        "%Y/%m/%d",
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


def today_moscow():
    return datetime.now(
        MOSCOW_TZ
    ).date()


# ============================================================
# ПРЕОБРАЗОВАНИЕ ФИО ВРАЧА
# ============================================================

def get_doctor_full_name(fio):
    """
    Преобразует:

    Вельмакина Ю.А.
    ->
    Вельмакина Юлия Александровна

    Колесников А.К.
    ->
    Колесников Александр Колесникович

    Если полное имя уже указано, возвращает его.
    """

    text = str(fio or "").strip()

    if not text:
        return ""


    # --------------------------------------------------------
    # Если уже полное ФИО
    # --------------------------------------------------------

    parts = text.split()

    if len(parts) >= 3:

        # Если второе и третье слова не похожи
        # на инициалы — считаем их именем и отчеством.
        if not (
            len(parts[1].replace(".", "")) <= 2
            and len(parts[2].replace(".", "")) <= 2
        ):
            return " ".join(parts[:3])


    # --------------------------------------------------------
    # Находим фамилию и инициалы
    # --------------------------------------------------------

    clean = text.replace(".", " ")
    clean = re.sub(r"\s+", " ", clean).strip()

    parts = clean.split()

    if len(parts) < 2:
        return text


    surname = ""
    initials = ""


    # Вариант:
    # Вельмакина Ю А

    if (
        len(parts) >= 3
        and len(parts[1]) == 1
        and len(parts[2]) == 1
    ):
        surname = parts[0]
        initials = parts[1] + parts[2]


    # Вариант:
    # Ю А Вельмакина

    elif (
        len(parts) >= 3
        and len(parts[0]) == 1
        and len(parts[1]) == 1
    ):
        surname = parts[2]
        initials = parts[0] + parts[1]


    # Вариант:
    # Вельмакина ЮА

    elif (
        len(parts) >= 2
        and len(parts[1]) == 2
    ):
        surname = parts[0]
        initials = parts[1]


    else:
        return text


    initials = initials.lower()


    # --------------------------------------------------------
    # Словарь имён
    # --------------------------------------------------------

    names = {
        "а": {
            "а": "Александр",
            "б": "Александра",
            "в": "Александр",
            "г": "Александра",
            "д": "Александр",
            "е": "Александра",
            "ж": "Александр",
            "з": "Александра",
            "и": "Александр",
            "к": "Александр",
            "л": "Александра",
            "м": "Александр",
            "н": "Александра",
            "о": "Александр",
            "п": "Александра",
            "р": "Александр",
            "с": "Александра",
            "т": "Александр",
            "у": "Александра",
            "ф": "Александр",
            "х": "Александра",
            "ц": "Александр",
            "ч": "Александра",
            "ш": "Александр",
            "щ": "Александра",
            "э": "Александр",
            "ю": "Александра",
            "я": "Александр",
        },
        "б": {},
    }


    # --------------------------------------------------------
    # Точный словарь известных врачей проекта.
    # Используется для корректного обращения.
    # --------------------------------------------------------

    known_doctors = {
        "анопко ас": (
            "Анопко",
            "Александр",
            "Сергеевич",
        ),

        "анопко а с": (
            "Анопко",
            "Александр",
            "Сергеевич",
        ),

        "вельмакина юа": (
            "Вельмакина",
            "Юлия",
            "Александровна",
        ),

        "вельмакина ю а": (
            "Вельмакина",
            "Юлия",
            "Александровна",
        ),

        "колесников ак": (
            "Колесников",
            "Александр",
            "Константинович",
        ),

        "колесников а к": (
            "Колесников",
            "Александр",
            "Константинович",
        ),

        "миндель тв": (
            "Миндель",
            "Татьяна",
            "Владимировна",
        ),

        "миндель т в": (
            "Миндель",
            "Татьяна",
            "Владимировна",
        ),

        "нецветаева ов": (
            "Нецветаева",
            "Ольга",
            "Викторовна",
        ),

        "нецветаева о в": (
            "Нецветаева",
            "Ольга",
            "Викторовна",
        ),

        "эльдяева гд": (
            "Эльдяева",
            "Галина",
            "Дмитриевна",
        ),

        "эльдяева г д": (
            "Эльдяева",
            "Галина",
            "Дмитриевна",
        ),

        "качанкин аа": (
            "Качанкин",
            "Алексей",
            "Александрович",
        ),

        "качанкин а а": (
            "Качанкин",
            "Алексей",
            "Александрович",
        ),
    }


    normalized = normalize_text(text)

    if normalized in known_doctors:

        surname, name, patronymic = known_doctors[
            normalized
        ]

        return f"{surname} {name} {patronymic}"


    # --------------------------------------------------------
    # Если врач не входит в известный словарь,
    # оставляем исходное ФИО.
    # --------------------------------------------------------

    return text


def get_greeting(fio):
    """
    Возвращает:

    Уважаемая Юлия Александровна!

    или

    Уважаемый Александр Константинович!
    """

    full_name = get_doctor_full_name(fio)

    parts = full_name.split()

    if len(parts) >= 3:

        name = parts[1]
        patronymic = parts[2]

        female_patronymic = (
            patronymic.endswith("овна")
            or patronymic.endswith("евна")
            or patronymic.endswith("ична")
            or patronymic.endswith("инична")
            or patronymic.endswith("ович") is False
            and patronymic.endswith("евич") is False
        )

        if female_patronymic:
            return f"Уважаемая {name} {patronymic}!"

        return f"Уважаемый {name} {patronymic}!"

    return f"Уважаемый(ая) {fio}!"


# ============================================================
# СОПОСТАВЛЕНИЕ ВРАЧЕЙ
# ============================================================

def get_signature(fio):
    """
    Нецветаева О.В.
    ->
    нецветаева ов

    О.В. Нецветаева
    ->
    нецветаева ов

    Нецветаева Ольга Викторовна
    ->
    нецветаева ов
    """

    text = normalize_text(fio)

    if not text:
        return ""

    parts = text.split()

    initials = ""
    surname = ""


    # Нецветаева О.В.

    if len(parts) >= 2:

        if re.fullmatch(
            r"[а-яa-z]\s*[.]?\s*[а-яa-z]\s*[.]?",
            parts[1]
        ):
            surname = parts[0]
            initials = re.sub(
                r"[^а-яa-z]",
                "",
                parts[1]
            )


    # О.В. Нецветаева

    if not surname and len(parts) >= 2:

        if re.fullmatch(
            r"[а-яa-z]\s*[.]?\s*[а-яa-z]\s*[.]?",
            parts[0]
        ):
            surname = parts[1]
            initials = re.sub(
                r"[^а-яa-z]",
                "",
                parts[0]
            )


    # Нецветаева Ольга Викторовна

    if not surname and len(parts) >= 3:

        surname = parts[0]

        first_name = parts[1]
        patronymic = parts[2]

        if first_name and patronymic:

            initials = (
                first_name[0]
                + patronymic[0]
            )


    if surname and initials:

        return f"{surname} {initials}"

    return text


def doctor_matches(input_name, db_name):

    a = normalize_text(input_name)
    b = normalize_text(db_name)

    if not a or not b:
        return False

    # 1. Полное совпадение

    if a == b:
        return True


    # 2. Сигнатура

    sig_a = get_signature(input_name)
    sig_b = get_signature(db_name)

    if (
        sig_a
        and sig_b
        and sig_a == sig_b
    ):
        return True


    # 3. Фамилия + инициалы

    parts_a = a.split()
    parts_b = b.split()

    surname_a = (
        parts_a[0]
        if parts_a
        else ""
    )

    surname_b = (
        parts_b[0]
        if parts_b
        else ""
    )

    if surname_a == surname_b:

        initials_a = re.sub(
            r"[^а-яa-z]",
            "",
            "".join(parts_a[1:])
        )

        initials_b = re.sub(
            r"[^а-яa-z]",
            "",
            "".join(parts_b[1:])
        )

        if initials_a and initials_b:

            if (
                initials_a.startswith(
                    initials_b[:1]
                )
                or initials_b.startswith(
                    initials_a[:1]
                )
            ):
                return True


    # 4. Частичное совпадение фамилии

    if (
        len(surname_a) >= 4
        and len(surname_b) >= 4
    ):

        if (
            surname_a in surname_b
            or surname_b in surname_a
        ):
            return True


    return False


def find_doctor_email(doctor_name, doctors):

    doctor_name = str(
        doctor_name or ""
    ).strip()

    if not doctor_name:
        return None

    for doctor in doctors:

        if doctor_matches(
            doctor["fio"],
            doctor_name
        ):

            email = normalize_email(
                doctor["email"]
            )

            if email:
                return email

            return None

    return None


# ============================================================
# GOOGLE SHEETS
# ============================================================

def connect_google():

    service_account_json = os.environ[
        "GOOGLE_SERVICE_ACCOUNT"
    ]

    credentials_info = json.loads(
        service_account_json
    )

    credentials = (
        Credentials.from_service_account_info(
            credentials_info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
            ],
        )
    )

    client = gspread.authorize(
        credentials
    )

    return client


def read_full_tasks_sheet(tasks_sheet):
    return tasks_sheet.get_all_values()


def load_doctors(doctors_sheet):

    data = doctors_sheet.get_all_values()

    doctors = []

    for row in data[1:]:

        if not row:
            continue

        fio = (
            row[0].strip()
            if len(row) > 0
            else ""
        )

        email = (
            row[COL_DOCTOR_EMAIL].strip()
            if len(row) > COL_DOCTOR_EMAIL
            else ""
        )

        if fio:

            doctors.append({
                "fio": fio,
                "email": email,
            })

    return doctors


# ============================================================
# СБОР ЗАДАНИЙ
# ============================================================

def collect_tasks(
    data,
    doctors,
    target_date
):

    grouped = {}

    for row_number, row in enumerate(
        data[1:],
        start=2
    ):

        # Минимум 13 колонок

        row = (
            row
            + [""] * max(
                0,
                13 - len(row)
            )
        )


        # Уже обработано

        completion = row[
            COL_COMPLETION
        ]

        if not is_empty_completion(
            completion
        ):
            continue


        # Дата

        row_date = parse_date(
            row[COL_DATE]
        )

        if row_date is None:
            continue

        if row_date != target_date:
            continue


        # Филиал

        if not is_branch_2(
            row[COL_BRANCH]
        ):
            continue


        # Врач

        doctor_name = row[
            COL_DOCTOR
        ].strip()

        if not doctor_name:
            doctor_name = (
                "НЕИЗВЕСТНЫЙ ВРАЧ"
            )


        # ----------------------------------------------------
        # Задания G / I / K
        # ----------------------------------------------------

        task_items = []

        for col in (
            COL_TASK1,
            COL_TASK2,
            COL_TASK3
        ):

            task = str(
                row[col]
            ).strip()

            if task:

                task_items.append({
                    "name": TASK_NAMES[col],
                    "text": task,
                })


        if not task_items:
            continue


        # Email

        email = find_doctor_email(
            doctor_name,
            doctors
        )

        if not email:
            email = RESERVE_EMAIL


        # Ключ врача

        key = normalize_text(
            doctor_name
        )


        if key not in grouped:

            grouped[key] = {
                "doctor_name": doctor_name,
                "email": email,
                "rows": [],
                "patients": [],
            }


        grouped[key]["rows"].append(
            row_number
        )


        # ----------------------------------------------------
        # Сохраняем данные пациента
        # ----------------------------------------------------

        grouped[key]["patients"].append({
            "patient_name": row[
                COL_PATIENT_NAME
            ].strip(),

            "patient_id": row[
                COL_PATIENT_ID
            ].strip(),

            "tasks": task_items,
        })


    return grouped


# ============================================================
# HTML-ПИСЬМО
# ============================================================

def create_html_email(
    doctor_name,
    patients,
    sent_at
):

    greeting = get_greeting(
        doctor_name
    )

    blocks = []


    for index, patient in enumerate(
        patients,
        start=1
    ):

        patient_name = html.escape(
            patient["patient_name"]
        )

        patient_id = html.escape(
            patient["patient_id"]
        )


        tasks_html = []

        for task in patient["tasks"]:

            task_name = html.escape(
                task["name"]
            )

            task_text = html.escape(
                task["text"]
            )

            tasks_html.append(
                f"""
                <div style="
                    margin: 8px 0 14px 0;
                ">
                    <div style="
                        font-weight: bold;
                        margin-bottom: 4px;
                    ">
                        {task_name}:
                    </div>

                    <div>
                        {task_text}
                    </div>
                </div>
                """
            )


        blocks.append(
            f"""
            <div style="
                margin-bottom: 20px;
                padding-bottom: 12px;
                border-bottom: 1px solid #cccccc;
            ">

                <div style="
                    font-weight: bold;
                    font-size: 16px;
                    margin-bottom: 5px;
                ">
                    {index}. {patient_name}
                </div>

                <div style="
                    margin-bottom: 10px;
                ">
                    ID/ОМС: {patient_id}
                </div>

                {''.join(tasks_html)}

            </div>
            """
        )


    total_tasks = sum(
        len(patient["tasks"])
        for patient in patients
    )


    return f"""
    <html>

    <body style="
        font-family: Arial, sans-serif;
        font-size: 14px;
        line-height: 1.45;
    ">

        <p>
            {html.escape(greeting)}
        </p>

        <p>
            Вам необходимо выполнить важные задания
            по записанным на сегодня к вам пациентам:
        </p>

        {''.join(blocks)}

        <p>
            <b>Всего пациентов:</b>
            {len(patients)}
        </p>

        <p>
            <b>Всего заданий:</b>
            {total_tasks}
        </p>

        <p>
            Прошу вернуть отработанное задание
            вашему заведующему.
        </p>

        <p>
            С уважением,<br>
            <b>BoTon и Агаввердиев Э.М.</b><br>
            {sent_at}
        </p>

    </body>

    </html>
    """


# ============================================================
# EMAIL
# ============================================================

def send_email(
    smtp,
    to_email,
    doctor_name,
    patients
):

    sent_at = datetime.now(
        MOSCOW_TZ
    ).strftime(
        "%d.%m.%Y %H:%M"
    )

    message = MIMEMultipart(
        "alternative"
    )

    message["From"] = os.environ[
        "SMTP_FROM_EMAIL"
    ]

    message["To"] = to_email

    message["Subject"] = (
        "Важные задания на "
        f"{today_moscow().strftime('%d.%m.%Y')}"
    )


    html_body = create_html_email(
        doctor_name,
        patients,
        sent_at
    )


    # Обычный текст — для почтовых клиентов,
    # которые не отображают HTML.

    text_blocks = []

    greeting = get_greeting(
        doctor_name
    )

    text_blocks.append(
        greeting
    )

    text_blocks.append(
        ""
    )

    text_blocks.append(
        "Вам необходимо выполнить важные "
        "задания по записанным на сегодня "
        "к вам пациентам:"
    )

    text_blocks.append("")


    total_tasks = 0


    for index, patient in enumerate(
        patients,
        start=1
    ):

        text_blocks.append(
            f"{index}. {patient['patient_name']}"
        )

        text_blocks.append(
            f"ID/ОМС: {patient['patient_id']}"
        )

        text_blocks.append("")


        for task in patient["tasks"]:

            text_blocks.append(
                f"{task['name']}:"
            )

            text_blocks.append(
                task["text"]
            )

            text_blocks.append("")

            total_tasks += 1


        text_blocks.append(
            "------------------------------"
        )

        text_blocks.append("")


    text_blocks.append(
        f"Всего пациентов: {len(patients)}"
    )

    text_blocks.append(
        f"Всего заданий: {total_tasks}"
    )

    text_blocks.append("")

    text_blocks.append(
        "Прошу вернуть отработанное задание "
        "вашему заведующему."
    )

    text_blocks.append("")

    text_blocks.append(
        "С уважением,"
    )

    text_blocks.append(
        "BoTon и Агаввердиев Э.М."
    )

    text_blocks.append(
        sent_at
    )


    text_body = "\n".join(
        text_blocks
    )


    message.attach(
        MIMEText(
            text_body,
            "plain",
            "utf-8"
        )
    )

    message.attach(
        MIMEText(
            html_body,
            "html",
            "utf-8"
        )
    )


    smtp.sendmail(
        message["From"],
        [to_email],
        message.as_string()
    )


def connect_smtp():

    host = os.environ[
        "SMTP_HOST"
    ]

    port = int(
        os.environ[
            "SMTP_PORT"
        ]
    )

    user = os.environ[
        "SMTP_USER"
    ]

    password = os.environ[
        "SMTP_PASSWORD"
    ]


    smtp = smtplib.SMTP(
        host,
        port,
        timeout=30
    )

    smtp.ehlo()

    smtp.starttls()

    smtp.ehlo()

    smtp.login(
        user,
        password
    )

    return smtp


# ============================================================
# СТАТУСЫ
# ============================================================

def make_status(email):

    now = datetime.now(
        MOSCOW_TZ
    )

    return (
        f"✅ Отправлено "
        f"{now.strftime('%d.%m.%Y %H:%M')} "
        f"на {email}"
    )


def make_reserve_status():

    now = datetime.now(
        MOSCOW_TZ
    )

    return (
        f"✅ Отправлено "
        f"{now.strftime('%d.%m.%Y %H:%M')} "
        f"на резервную почту"
    )


def make_error_status():

    now = datetime.now(
        MOSCOW_TZ
    )

    return (
        f"❌ Ошибка отправки "
        f"{now.strftime('%d.%m.%Y %H:%M')}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(f"BOTON v{VERSION}")
    print(f"TEST_MODE = {TEST_MODE}")
    print("=" * 60)


    # --------------------------------------------------------
    # Google Sheets
    # --------------------------------------------------------

    try:

        client = connect_google()


        tasks_book = client.open_by_key(
            TASKS_SHEET_ID
        )

        tasks_sheet = tasks_book.worksheet(
            TASKS_SHEET_NAME
        )


        doctors_book = client.open_by_key(
            DOCTORS_SHEET_ID
        )

        doctors_sheet = doctors_book.worksheet(
            DOCTORS_SHEET_NAME
        )


        print(
            "Google Sheets: OK"
        )


    except Exception as e:

        print(
            "Ошибка Google Sheets: "
            f"{type(e).__name__}: {e}"
        )

        raise


    # --------------------------------------------------------
    # Чтение
    # --------------------------------------------------------

    data = read_full_tasks_sheet(
        tasks_sheet
    )

    doctors = load_doctors(
        doctors_sheet
    )


    print(
        f"Прочитано строк: {len(data)}"
    )

    print(
        f"Врачей в базе: {len(doctors)}"
    )


    # --------------------------------------------------------
    # Сегодня
    # --------------------------------------------------------

    target_date = today_moscow()

    print(
        "Дата:",
        target_date.strftime(
            "%d.%m.%Y"
        )
    )


    grouped = collect_tasks(
        data,
        doctors,
        target_date
    )


    # --------------------------------------------------------
    # Нет новых заданий
    # --------------------------------------------------------

    if not grouped:

        print(
            "Новых заданий нет."
        )

        print(
            "Проверка завершена."
        )

        return


    total_rows = sum(
        len(item["rows"])
        for item in grouped.values()
    )


    total_tasks = sum(
        len(patient["tasks"])
        for item in grouped.values()
        for patient in item["patients"]
    )


    print(
        f"Новых врачей: {len(grouped)}"
    )

    print(
        f"Строк с заданиями: {total_rows}"
    )

    print(
        f"Всего заданий: {total_tasks}"
    )


    # --------------------------------------------------------
    # SMTP
    # --------------------------------------------------------

    print(
        "Подключение к SMTP..."
    )


    try:

        smtp = connect_smtp()

        print(
            "SMTP: OK"
        )

    except Exception as e:

        print(
            "Ошибка подключения SMTP: "
            f"{type(e).__name__}: {e}"
        )

        raise


    updates = []

    doctors_sent = 0
    reserve_sent = 0
    errors = 0


    try:

        for item in grouped.values():

            doctor_name = item[
                "doctor_name"
            ]

            doctor_email = item[
                "email"
            ]

            patients = item[
                "patients"
            ]

            row_numbers = item[
                "rows"
            ]


            # ------------------------------------------------
            # TEST MODE
            # ------------------------------------------------

            if TEST_MODE:

                send_email(
                    smtp,
                    os.environ[
                        "SMTP_USER"
                    ],
                    doctor_name,
                    patients
                )

                print(
                    "Тестовое письмо отправлено."
                )

                continue


            # ------------------------------------------------
            # Отправка врачу
            # ------------------------------------------------

            try:

                send_email(
                    smtp,
                    doctor_email,
                    doctor_name,
                    patients
                )


                if doctor_email == RESERVE_EMAIL:

                    status = make_reserve_status()

                    reserve_sent += 1

                else:

                    status = make_status(
                        doctor_email
                    )

                    doctors_sent += 1


                for row_number in row_numbers:

                    updates.append({
                        "range": f"M{row_number}",
                        "values": [[status]],
                    })


                print(
                    "Письмо отправлено."
                )


            # ------------------------------------------------
            # Ошибка → резерв
            # ------------------------------------------------

            except Exception:

                print(
                    "Ошибка отправки врачу. "
                    "Используется резерв."
                )


                try:

                    send_email(
                        smtp,
                        RESERVE_EMAIL,
                        doctor_name,
                        patients
                    )


                    status = make_reserve_status()


                    for row_number in row_numbers:

                        updates.append({
                            "range": f"M{row_number}",
                            "values": [[status]],
                        })


                    reserve_sent += 1


                    print(
                        "Отправлено на резерв."
                    )


                except Exception:

                    status = make_error_status()


                    for row_number in row_numbers:

                        updates.append({
                            "range": f"M{row_number}",
                            "values": [[status]],
                        })


                    errors += 1


                    print(
                        "Ошибка резервной отправки."
                    )


    finally:

        smtp.quit()


    # --------------------------------------------------------
    # Запись статусов
    # --------------------------------------------------------

    if updates and not TEST_MODE:

        tasks_sheet.batch_update(
            updates,
            value_input_option="USER_ENTERED"
        )


    # --------------------------------------------------------
    # Итог
    # --------------------------------------------------------

    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)


    print(
        "Врачей обработано:",
        len(grouped)
    )

    print(
        "Успешно отправлено врачам:",
        doctors_sent
    )

    print(
        "Отправлено на резерв:",
        reserve_sent
    )

    print(
        "Ошибок:",
        errors
    )

    print(
        "Строк записано в M:",
        len(updates)
    )

    print(
        "Всего заданий:",
        total_tasks
    )

    print("=" * 60)

    print(
        f"BOTON v{VERSION} завершён."
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
