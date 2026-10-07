import os
import re
import json
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# BOTON v1.5
# ============================================================

VERSION = "1.5"

TEST_MODE = False

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

TASKS_SHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_SHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

RESERVE_EMAIL = "eagaverdiev@mosmedzdrav.ru"


# ------------------------------------------------------------
# Колонки таблицы заданий
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# Колонки базы врачей
# A = ФИО
# B = Chat ID
# C = Email
# ------------------------------------------------------------

COL_DOCTOR_EMAIL = 2


# ------------------------------------------------------------
# Филиал №2
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

    return any(keyword in branch for keyword in BRANCH_KEYWORDS)


def is_empty_completion(value):
    """
    Пустой статус считается необработанным.
    Значения null/undefined также считаются пустыми.
    """
    value = normalize_text(value)

    return value in ("", "null", "undefined")


def parse_date(value):
    """Преобразование даты из таблицы в dd.mm.yyyy."""
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
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


def today_moscow():
    return datetime.now(MOSCOW_TZ).date()


# ============================================================
# СОПОСТАВЛЕНИЕ ВРАЧЕЙ
# ============================================================

def get_signature(fio):
    """
    Получает подпись врача:
    Нецветаева О.В. -> нецветаева ов
    О.В. Нецветаева -> нецветаева ов
    Нецветаева Ольга Викторовна -> нецветаева ов
    """

    text = normalize_text(fio)

    if not text:
        return ""

    parts = text.split()

    initials = ""
    surname = ""

    # Вариант:
    # Нецветаева О.В.
    if len(parts) >= 2:
        if re.fullmatch(r"[а-яa-z]\s*[.]?\s*[а-яa-z]\s*[.]?", parts[1]):
            surname = parts[0]
            initials = re.sub(r"[^а-яa-z]", "", parts[1])

    # Вариант:
    # О.В. Нецветаева
    if not surname and len(parts) >= 2:
        if re.fullmatch(r"[а-яa-z]\s*[.]?\s*[а-яa-z]\s*[.]?", parts[0]):
            surname = parts[1]
            initials = re.sub(r"[^а-яa-z]", "", parts[0])

    # Вариант:
    # Нецветаева Ольга Викторовна
    if not surname and len(parts) >= 3:
        surname = parts[0]

        first_name = parts[1]
        patronymic = parts[2]

        if first_name and patronymic:
            initials = first_name[0] + patronymic[0]

    if surname and initials:
        return f"{surname} {initials}"

    return text


def doctor_matches(input_name, db_name):
    """
    Надёжное сопоставление врача.
    """

    a = normalize_text(input_name)
    b = normalize_text(db_name)

    if not a or not b:
        return False

    # 1. Полное совпадение нормализованных строк
    if a == b:
        return True

    # 2. Совпадение сигнатур
    sig_a = get_signature(input_name)
    sig_b = get_signature(db_name)

    if sig_a and sig_b and sig_a == sig_b:
        return True

    # 3. Фамилия + совместимые инициалы
    parts_a = a.split()
    parts_b = b.split()

    surname_a = parts_a[0] if parts_a else ""
    surname_b = parts_b[0] if parts_b else ""

    if surname_a == surname_b:
        initials_a = re.sub(r"[^а-яa-z]", "", "".join(parts_a[1:]))
        initials_b = re.sub(r"[^а-яa-z]", "", "".join(parts_b[1:]))

        if initials_a and initials_b:
            if (
                initials_a.startswith(initials_b[:1])
                or initials_b.startswith(initials_a[:1])
            ):
                return True

    # 4. Частичное совпадение фамилии
    if len(surname_a) >= 4 and len(surname_b) >= 4:
        if surname_a in surname_b or surname_b in surname_a:
            return True

    return False


def find_doctor_email(doctor_name, doctors):
    """
    Возвращает email врача.
    Если врач не найден — резервная почта.
    """

    doctor_name = str(doctor_name or "").strip()

    if not doctor_name:
        return None

    for doctor in doctors:
        if doctor_matches(doctor["fio"], doctor_name):
            email = normalize_email(doctor["email"])

            if email:
                return email

            return None

    return None


# ============================================================
# GOOGLE SHEETS
# ============================================================

def connect_google():
    service_account_json = os.environ["GOOGLE_SERVICE_ACCOUNT"]

    credentials_info = json.loads(service_account_json)

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )

    client = gspread.authorize(credentials)

    return client


def read_full_tasks_sheet(tasks_sheet):
    """
    Читаем фактически существующие строки.
    row_count — физическая ёмкость листа, поэтому используем
    get_all_values(), а не row_count.
    """

    return tasks_sheet.get_all_values()


def load_doctors(doctors_sheet):
    data = doctors_sheet.get_all_values()

    doctors = []

    for row in data[1:]:
        if not row:
            continue

        fio = row[0].strip() if len(row) > 0 else ""
        email = row[COL_DOCTOR_EMAIL].strip() if len(row) > COL_DOCTOR_EMAIL else ""

        if fio:
            doctors.append({
                "fio": fio,
                "email": email,
            })

    return doctors


# ============================================================
# ЗАДАНИЯ
# ============================================================

def collect_tasks(data, doctors, target_date):
    """
    Возвращает сгруппированные задания:

    {
        doctor_key: {
            "doctor_name": "...",
            "email": "...",
            "rows": [...],
            "tasks": [...]
        }
    }
    """

    grouped = {}

    for row_number, row in enumerate(data[1:], start=2):

        # Гарантируем минимум 13 колонок
        row = row + [""] * max(0, 13 - len(row))

        # Уже обработано?
        completion = row[COL_COMPLETION]

        if not is_empty_completion(completion):
            continue

        # Дата
        row_date = parse_date(row[COL_DATE])

        if row_date is None:
            continue

        if row_date != target_date:
            continue

        # Филиал
        if not is_branch_2(row[COL_BRANCH]):
            continue

        doctor_name = row[COL_DOCTOR].strip()

        if not doctor_name:
            doctor_name = "НЕИЗВЕСТНЫЙ ВРАЧ"

        # Задания G / I / K
        tasks = []

        for col in (COL_TASK1, COL_TASK2, COL_TASK3):
            task = str(row[col]).strip()

            if task:
                tasks.append(task)

        if not tasks:
            continue

        # Email
        email = find_doctor_email(doctor_name, doctors)

        if not email:
            email = RESERVE_EMAIL

        # Группировка по врачу
        key = normalize_text(doctor_name)

        if key not in grouped:
            grouped[key] = {
                "doctor_name": doctor_name,
                "email": email,
                "rows": [],
                "tasks": [],
            }

        grouped[key]["rows"].append(row_number)

        for task in tasks:
            grouped[key]["tasks"].append({
                "row": row_number,
                "text": task,
            })

    return grouped


# ============================================================
# EMAIL
# ============================================================

def create_html_email(doctor_name, tasks):
    """
    HTML письма.
    Данные пациентов и текст заданий используются только
    внутри письма, но не выводятся в GitHub Actions log.
    """

    rows = []

    for item in tasks:
        rows.append(
            f"""
            <tr>
                <td style="padding:6px;border:1px solid #ccc;">
                    {item["text"]}
                </td>
            </tr>
            """
        )

    tasks_html = "".join(rows)

    return f"""
    <html>
    <body>
        <p>Уважаемый(ая) {doctor_name}!</p>

        <p>
            Вам направлены задания для обработки:
        </p>

        <table style="border-collapse:collapse;">
            {tasks_html}
        </table>

        <p>
            BoTon v{VERSION}
        </p>
    </body>
    </html>
    """


def send_email(smtp, to_email, doctor_name, tasks):
    message = MIMEMultipart("alternative")

    message["From"] = os.environ["SMTP_FROM_EMAIL"]
    message["To"] = to_email
    message["Subject"] = f"Задания BoTon для {doctor_name}"

    html = create_html_email(
        doctor_name,
        tasks,
    )

    message.attach(
        MIMEText(html, "html", "utf-8")
    )

    smtp.sendmail(
        message["From"],
        [to_email],
        message.as_string(),
    )


def connect_smtp():
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    smtp = smtplib.SMTP(host, port, timeout=30)

    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()

    smtp.login(user, password)

    return smtp


# ============================================================
# СТАТУСЫ
# ============================================================

def make_status(email):
    now = datetime.now(MOSCOW_TZ)

    return (
        f"✅ Отправлено "
        f"{now.strftime('%d.%m.%Y %H:%M')} "
        f"на {email}"
    )


def make_reserve_status():
    now = datetime.now(MOSCOW_TZ)

    return (
        f"✅ Отправлено "
        f"{now.strftime('%d.%m.%Y %H:%M')} "
        f"на резервную почту"
    )


def make_error_status():
    now = datetime.now(MOSCOW_TZ)

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
    # Google
    # --------------------------------------------------------

    try:
        client = connect_google()

        tasks_book = client.open_by_key(TASKS_SHEET_ID)
        tasks_sheet = tasks_book.worksheet(TASKS_SHEET_NAME)

        doctors_book = client.open_by_key(DOCTORS_SHEET_ID)
        doctors_sheet = doctors_book.worksheet(DOCTORS_SHEET_NAME)

        print("Google Sheets: OK")

    except Exception as e:
        print(f"Ошибка Google Sheets: {type(e).__name__}: {e}")
        raise

    # --------------------------------------------------------
    # Читаем таблицы
    # --------------------------------------------------------

    data = read_full_tasks_sheet(tasks_sheet)

    doctors = load_doctors(doctors_sheet)

    print(f"Прочитано строк: {len(data)}")
    print(f"Врачей в базе: {len(doctors)}")

    # --------------------------------------------------------
    # Сегодня
    # --------------------------------------------------------

    target_date = today_moscow()

    print(
        "Дата:",
        target_date.strftime("%d.%m.%Y")
    )

    grouped = collect_tasks(
        data,
        doctors,
        target_date,
    )

    # --------------------------------------------------------
    # Нет новых заданий
    # --------------------------------------------------------

    if not grouped:
        print("Новых заданий нет.")
        print("Проверка завершена.")
        return

    total_rows = sum(
        len(item["rows"])
        for item in grouped.values()
    )

    total_tasks = sum(
        len(item["tasks"])
        for item in grouped.values()
    )

    print(f"Новых врачей: {len(grouped)}")
    print(f"Строк с заданиями: {total_rows}")
    print(f"Всего заданий: {total_tasks}")

    # --------------------------------------------------------
    # SMTP
    # --------------------------------------------------------

    print("Подключение к SMTP...")

    try:
        smtp = connect_smtp()
        print("SMTP: OK")
    except Exception as e:
        print(
            f"Ошибка подключения SMTP: "
            f"{type(e).__name__}: {e}"
        )
        raise

    updates = []

    doctors_sent = 0
    reserve_sent = 0
    errors = 0

    try:

        for item in grouped.values():

            doctor_name = item["doctor_name"]
            doctor_email = item["email"]
            tasks = item["tasks"]
            row_numbers = item["rows"]

            # ------------------------------------------------
            # TEST_MODE
            # ------------------------------------------------

            if TEST_MODE:

                send_email(
                    smtp,
                    os.environ["SMTP_USER"],
                    doctor_name,
                    tasks,
                )

                print("Тестовое письмо отправлено.")

                continue

            # ------------------------------------------------
            # Обычная отправка врачу
            # ------------------------------------------------

            try:

                send_email(
                    smtp,
                    doctor_email,
                    doctor_name,
                    tasks,
                )

                status = (
                    make_reserve_status()
                    if doctor_email == RESERVE_EMAIL
                    else make_status(doctor_email)
                )

                for row_number in row_numbers:
                    updates.append({
                        "range": f"M{row_number}",
                        "values": [[status]],
                    })

                if doctor_email == RESERVE_EMAIL:
                    reserve_sent += 1
                else:
                    doctors_sent += 1

                print("Письмо отправлено.")

            # ------------------------------------------------
            # Ошибка отправки врачу
            # ------------------------------------------------

            except Exception:

                print("Ошибка отправки врачу. Используется резерв.")

                try:

                    send_email(
                        smtp,
                        RESERVE_EMAIL,
                        doctor_name,
                        tasks,
                    )

                    status = make_reserve_status()

                    for row_number in row_numbers:
                        updates.append({
                            "range": f"M{row_number}",
                            "values": [[status]],
                        })

                    reserve_sent += 1

                    print("Отправлено на резерв.")

                except Exception:

                    status = make_error_status()

                    for row_number in row_numbers:
                        updates.append({
                            "range": f"M{row_number}",
                            "values": [[status]],
                        })

                    errors += 1

                    print("Ошибка резервной отправки.")

    finally:
        smtp.quit()

    # --------------------------------------------------------
    # Записываем статусы одной пакетной операцией
    # --------------------------------------------------------

    if updates and not TEST_MODE:

        tasks_sheet.batch_update(
            updates,
            value_input_option="USER_ENTERED",
        )

    # --------------------------------------------------------
    # Итог
    # --------------------------------------------------------

    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)

    print(
        "Врачей обработано:",
        len(grouped),
    )

    print(
        "Успешно отправлено врачам:",
        doctors_sent,
    )

    print(
        "Отправлено на резерв:",
        reserve_sent,
    )

    print(
        "Ошибок:",
        errors,
    )

    print(
        "Строк записано в M:",
        len(updates),
    )

    print(
        "Всего заданий:",
        total_tasks,
    )

    print("=" * 60)
    print(f"BOTON v{VERSION} завершён.")
    print("=" * 60)


if __name__ == "__main__":
    main()
