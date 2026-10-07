import os
import re
import smtplib
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# BOTON v1.4
# ============================================================

VERSION = "1.4"

# False = реальная отправка
# True  = тестовая отправка только на SMTP_USER
TEST_MODE = False

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ============================================================
# GOOGLE SHEETS
# ============================================================

TASKS_SHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_WORKSHEET = "Лист1"

DOCTORS_SHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_WORKSHEET = "Врачи"


# ============================================================
# КОЛОНКИ — 0-based
# ============================================================

COL_DATE = 0
COL_BRANCH = 1
COL_DOCTOR = 2
COL_PATIENT_ID = 3
COL_PATIENT_NAME = 4
COL_SOURCE = 5
COL_TASK1 = 6
COL_TASK2 = 8
COL_TASK3 = 10
COL_COMPLETION = 12

# База врачей:
# A = ФИО
# B = Chat ID
# C = Email
COL_DOCTOR_EMAIL = 2


# ============================================================
# ФИЛИАЛ 2
# ============================================================

BRANCH2_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]


# ============================================================
# РЕЗЕРВНАЯ ПОЧТА
# ============================================================

RESERVE_EMAIL = "eagaverdiev@mosmedzdrav.ru"


# ============================================================
# SMTP
# ============================================================

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ["SMTP_PORT"])
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
SMTP_FROM_EMAIL = os.environ["SMTP_FROM_EMAIL"]


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_text(value):
    if value is None:
        return ""

    value = str(value).strip().lower()
    value = value.replace("ё", "е")

    value = re.sub(r"\s+", " ", value)

    return value


def normalize_branch(value):
    value = normalize_text(value)

    value = value.replace("№", " ")
    value = re.sub(r"[()]", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def is_branch2(value):
    text = normalize_branch(value)

    for keyword in BRANCH2_KEYWORDS:
        if normalize_branch(keyword) in text:
            return True

    return False


def normalize_date(value):
    """
    Приводит различные варианты даты к YYYY-MM-DD.
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    if not text:
        return None

    # Убираем время, если оно есть
    text = text.split(" ")[0]

    formats = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    return None


def get_signature(fio):
    """
    Нормализует ФИО в формат:
    фамилия + инициалы

    Примеры:
    О.В. Нецветаева       -> нецветаева ов
    Нецветаева О.В.       -> нецветаева ов
    Нецветаева Ольга Викторовна -> нецветаева ов
    """

    text = normalize_text(fio)

    if not text:
        return ""

    # Убираем точки и лишние символы,
    # но сохраняем пробелы.
    cleaned = re.sub(r"[^а-яa-z\s-]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    parts = cleaned.split()

    if not parts:
        return ""

    # Если есть инициалы вида "о в"
    initials = []

    for part in parts:
        if len(part) == 1 and part.isalpha():
            initials.append(part)

    # Случай:
    # Нецветаева О В
    if initials:
        surname_candidates = [
            p for p in parts
            if len(p) > 1
        ]

        if surname_candidates:
            surname = surname_candidates[0]
            return surname + " " + "".join(initials[:2])

    # Инициалы могли быть слиты:
    # "О.В Нецветаева"
    original = normalize_text(fio)

    m = re.search(
        r"\b([а-яa-z])\s*[\.\s]\s*([а-яa-z])\b",
        original
    )

    if m:
        init = m.group(1) + m.group(2)

        words = re.findall(r"[а-яa-z-]+", original)

        surname = None

        for word in words:
            if len(word) > 2 and word not in {
                m.group(1),
                m.group(2)
            }:
                surname = word
                break

        if surname:
            return surname + " " + init

    # Полное ФИО:
    # Нецветаева Ольга Викторовна
    if len(parts) >= 3:
        surname = parts[0]

        first_name = parts[1]
        patronymic = parts[2]

        return (
            surname
            + " "
            + first_name[0]
            + patronymic[0]
        )

    # Фамилия + имя
    if len(parts) == 2:
        surname = parts[0]
        second = parts[1]

        if len(second) >= 1:
            return surname + " " + second[0]

    return normalize_text(fio)


def doctor_matches(sheet_fio, doctor_fio):
    """
    Надёжное сопоставление врача.

    1. Точное совпадение подписи.
    2. Одинаковая фамилия + совместимые инициалы.
    3. Частичное совпадение фамилии.
    """

    a = normalize_text(sheet_fio)
    b = normalize_text(doctor_fio)

    if not a or not b:
        return False

    sig_a = get_signature(a)
    sig_b = get_signature(b)

    # 1. Полное совпадение нормализованной подписи
    if sig_a and sig_b and sig_a == sig_b:
        return True

    # Разбираем подписи
    pa = sig_a.split()
    pb = sig_b.split()

    if len(pa) >= 2 and len(pb) >= 2:

        surname_a = pa[0]
        initials_a = pa[1]

        surname_b = pb[0]
        initials_b = pb[1]

        # 2. Одинаковая фамилия.
        # Инициалы могут быть полными или частичными.
        if surname_a == surname_b:

            if (
                initials_a.startswith(initials_b)
                or initials_b.startswith(initials_a)
            ):
                return True

    # 3. Частичное совпадение фамилии
    # Только если фамилии достаточно длинные.
    if len(pa) >= 1 and len(pb) >= 1:

        surname_a = pa[0]
        surname_b = pb[0]

        if (
            len(surname_a) >= 4
            and len(surname_b) >= 4
            and (
                surname_a in surname_b
                or surname_b in surname_a
            )
        ):
            return True

    return False


def build_doctor_db(doctors_data):
    """
    Возвращает список:
    [
        {
            "fio": "...",
            "email": "..."
        }
    ]
    """

    doctors = []

    for row in doctors_data[1:]:
        if not row:
            continue

        fio = row[0].strip() if len(row) > 0 else ""

        email = (
            row[COL_DOCTOR_EMAIL].strip()
            if len(row) > COL_DOCTOR_EMAIL
            else ""
        )

        if not fio:
            continue

        doctors.append({
            "fio": fio,
            "email": email,
        })

    return doctors


def find_doctor(sheet_fio, doctors):
    """
    Ищет врача в базе.
    """

    for doctor in doctors:
        if doctor_matches(sheet_fio, doctor["fio"]):
            return doctor

    return None


def get_tasks_from_row(row):
    """
    Забирает задания из G, I, K.
    """

    tasks = []

    for index in (
        COL_TASK1,
        COL_TASK2,
        COL_TASK3,
    ):
        if len(row) <= index:
            continue

        value = str(row[index]).strip()

        if value:
            tasks.append(value)

    return tasks


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


def send_email(smtp, to_email, doctor_name, tasks):
    """
    Отправляет письмо врачу.
    """

    message = MIMEMultipart("alternative")

    message["From"] = SMTP_FROM_EMAIL
    message["To"] = to_email
    message["Subject"] = (
        f"Задания врачу на "
        f"{datetime.now(MOSCOW_TZ).strftime('%d.%m.%Y')}"
    )

    html_items = ""

    for task in tasks:
        html_items += f"<li>{task}</li>"

    html = f"""
    <html>
      <body>
        <p>Здравствуйте, {doctor_name}.</p>

        <p>Задания на сегодня:</p>

        <ul>
          {html_items}
        </ul>

        <p>
          Система BoTon
        </p>
      </body>
    </html>
    """

    message.attach(
        MIMEText(html, "html", "utf-8")
    )

    smtp.sendmail(
        SMTP_FROM_EMAIL,
        [to_email],
        message.as_string(),
    )


def read_full_tasks_sheet(tasks_sheet):
    """
    Читаем явный диапазон A:M по физическому размеру листа.

    Google может не возвращать хвост полностью пустых строк,
    поэтому дополнительно используем row_count.
    """

    last_row = tasks_sheet.row_count

    if last_row < 1:
        return []

    return tasks_sheet.get(
        f"A1:M{last_row}"
    )


# ============================================================
# ОСНОВНАЯ ПРОГРАММА
# ============================================================

def main():

    print("=" * 60)
    print(f"BOTON v{VERSION}")
    print(f"TEST_MODE = {TEST_MODE}")
    print("=" * 60)

    # --------------------------------------------------------
    # GOOGLE AUTH
    # --------------------------------------------------------

    service_account_json = os.environ[
        "GOOGLE_SERVICE_ACCOUNT"
    ]

    credentials = Credentials.from_service_account_info(
        eval(service_account_json),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    gc = gspread.authorize(credentials)

    # --------------------------------------------------------
    # ОТКРЫВАЕМ ТАБЛИЦЫ
    # --------------------------------------------------------

    tasks_book = gc.open_by_key(
        TASKS_SHEET_ID
    )

    tasks_sheet = tasks_book.worksheet(
        TASKS_WORKSHEET
    )

    doctors_book = gc.open_by_key(
        DOCTORS_SHEET_ID
    )

    doctors_sheet = doctors_book.worksheet(
        DOCTORS_WORKSHEET
    )

    # --------------------------------------------------------
    # ЧТЕНИЕ
    # --------------------------------------------------------

    data = read_full_tasks_sheet(
        tasks_sheet
    )

    doctors_data = doctors_sheet.get_all_values()

    doctors = build_doctor_db(
        doctors_data
    )

    print("Google Sheets: OK")
    print("Прочитано строк:", len(data))
    print(
        "Физических строк листа:",
        tasks_sheet.row_count
    )
    print(
        "Врачей в базе:",
        len(doctors)
    )

    today = datetime.now(
        MOSCOW_TZ
    ).date()

    print(
        "Дата:",
        today.strftime("%d.%m.%Y")
    )

    # --------------------------------------------------------
    # ДИАГНОСТИКА ФИЛЬТРОВ
    # --------------------------------------------------------

    stats = {
        "empty_rows": 0,
        "bad_date": 0,
        "other_date": 0,
        "other_branch": 0,
        "already_done": 0,
        "no_doctor": 0,
        "doctor_not_found": 0,
        "no_tasks": 0,
        "accepted": 0,
    }

    # Для безопасной диагностики показываем только
    # технические значения.
    branch_counter = {}
    date_counter = {}

    # --------------------------------------------------------
    # ГРУППИРОВКА
    # --------------------------------------------------------

    grouped = {}

    for row_number, row in enumerate(
        data,
        start=1
    ):

        if not row:
            stats["empty_rows"] += 1
            continue

        # ----------------------------------------------------
        # Диагностика даты
        # ----------------------------------------------------

        raw_date = (
            row[COL_DATE]
            if len(row) > COL_DATE
            else ""
        )

        parsed_date = normalize_date(
            raw_date
        )

        if parsed_date is None:
            stats["bad_date"] += 1
        else:
            date_key = parsed_date.strftime(
                "%d.%m.%Y"
            )

            date_counter[date_key] = (
                date_counter.get(
                    date_key,
                    0
                ) + 1
            )

        # ----------------------------------------------------
        # Филиал
        # ----------------------------------------------------

        raw_branch = (
            row[COL_BRANCH]
            if len(row) > COL_BRANCH
            else ""
        )

        branch_key = normalize_branch(
            raw_branch
        )

        if branch_key:
            branch_counter[branch_key] = (
                branch_counter.get(
                    branch_key,
                    0
                ) + 1
            )

        # ----------------------------------------------------
        # Дата
        # ----------------------------------------------------

        if parsed_date != today:

            stats["other_date"] += 1
            continue

        # ----------------------------------------------------
        # Филиал
        # ----------------------------------------------------

        if not is_branch2(
            raw_branch
        ):

            stats["other_branch"] += 1
            continue

        # ----------------------------------------------------
        # M — статус
        # ----------------------------------------------------

        completion = (
            str(row[COL_COMPLETION]).strip()
            if len(row) > COL_COMPLETION
            else ""
        )

        completion_normalized = normalize_text(
            completion
        )

        if (
            completion_normalized
            and completion_normalized
            not in {
                "null",
                "undefined",
            }
        ):

            stats["already_done"] += 1
            continue

        # ----------------------------------------------------
        # ВРАЧ
        # ----------------------------------------------------

        doctor_fio = (
            str(row[COL_DOCTOR]).strip()
            if len(row) > COL_DOCTOR
            else ""
        )

        if not doctor_fio:

            stats["no_doctor"] += 1
            continue

        doctor = find_doctor(
            doctor_fio,
            doctors
        )

        if doctor is None:

            stats["doctor_not_found"] += 1
            continue

        # ----------------------------------------------------
        # ЗАДАНИЯ
        # ----------------------------------------------------

        tasks = get_tasks_from_row(
            row
        )

        if not tasks:

            stats["no_tasks"] += 1
            continue

        stats["accepted"] += 1

        # Ключ группировки — email.
        # Если email отсутствует,
        # используем ФИО врача.
        email = doctor["email"].strip()

        group_key = (
            email.lower()
            if email
            else normalize_text(
                doctor["fio"]
            )
        )

        if group_key not in grouped:

            grouped[group_key] = {
                "doctor_fio": doctor["fio"],
                "email": email,
                "tasks": [],
                "rows": [],
            }

        grouped[group_key]["tasks"].extend(
            tasks
        )

        grouped[group_key]["rows"].append(
            row_number
        )

    # --------------------------------------------------------
    # БЕЗОПАСНАЯ ДИАГНОСТИКА
    # --------------------------------------------------------

    print()
    print("----- ДИАГНОСТИКА -----")

    print(
        "Пустых строк:",
        stats["empty_rows"]
    )

    print(
        "Строк с нераспознанной датой:",
        stats["bad_date"]
    )

    print(
        "Строк с другой датой:",
        stats["other_date"]
    )

    print(
        "Строк других филиалов:",
        stats["other_branch"]
    )

    print(
        "Уже обработано по M:",
        stats["already_done"]
    )

    print(
        "Без ФИО врача:",
        stats["no_doctor"]
    )

    print(
        "Врач не найден в базе:",
        stats["doctor_not_found"]
    )

    print(
        "Без заданий:",
        stats["no_tasks"]
    )

    print(
        "Подошло для обработки:",
        stats["accepted"]
    )

    print()
    print("Даты в полученных данных:")

    for key in sorted(date_counter):
        print(
            f"  {key}: {date_counter[key]}"
        )

    print()
    print("Филиалы в полученных данных:")

    for key, count in sorted(
        branch_counter.items(),
        key=lambda x: (-x[1], x[0])
    ):

        # Чтобы лог не разрастался,
        # показываем максимум 30 вариантов.
        if count > 0:
            print(
                f"  {key}: {count}"
            )

    print()
    print(
        "Новых врачей:",
        len(grouped)
    )

    print(
        "Строк с заданиями:",
        sum(
            len(item["rows"])
            for item in grouped.values()
        )
    )

    print(
        "Всего заданий:",
        sum(
            len(item["tasks"])
            for item in grouped.values()
        )
    )

    # --------------------------------------------------------
    # ЕСЛИ НОВЫХ ЗАДАНИЙ НЕТ
    # --------------------------------------------------------

    if not grouped:

        print()
        print(
            "Новых заданий нет."
        )

        print(
            "Следующая проверка — "
            "по расписанию GitHub Actions."
        )

        return

    # --------------------------------------------------------
    # SMTP
    # --------------------------------------------------------

    print()
    print("Подключение к SMTP...")

    smtp = smtplib.SMTP_SSL(
        SMTP_HOST,
        SMTP_PORT,
        timeout=30,
    )

    smtp.login(
        SMTP_USER,
        SMTP_PASSWORD,
    )

    print("SMTP: OK")

    # --------------------------------------------------------
    # ТЕСТОВЫЙ РЕЖИМ
    # --------------------------------------------------------

    if TEST_MODE:

        print()
        print(
            "TEST_MODE=True:"
            " письма врачам НЕ отправляются."
        )

        test_tasks = []

        for item in grouped.values():
            test_tasks.extend(
                item["tasks"]
            )

        # В тестовом режиме отправляем
        # только сводное письмо себе.
        send_email(
            smtp,
            SMTP_USER,
            "Тест BoTon",
            test_tasks,
        )

        smtp.quit()

        print(
            "Тестовое письмо отправлено на SMTP_USER."
        )

        print(
            "Таблица НЕ изменялась."
        )

        return

    # --------------------------------------------------------
    # РЕАЛЬНАЯ РАССЫЛКА
    # --------------------------------------------------------

    updates = []

    sent_count = 0
    error_count = 0
    reserve_count = 0

    for item in grouped.values():

        doctor_fio = item["doctor_fio"]
        email = item["email"]
        tasks = item["tasks"]
        rows = item["rows"]

        # ----------------------------------------------------
        # Если email врача отсутствует —
        # сразу резервная почта.
        # ----------------------------------------------------

        if not email:

            try:

                send_email(
                    smtp,
                    RESERVE_EMAIL,
                    doctor_fio,
                    tasks,
                )

                status = make_reserve_status()

                reserve_count += 1

                print(
                    "Отправлено на резервную почту."
                )

            except Exception as e:

                status = make_error_status()

                error_count += 1

                print(
                    "Ошибка отправки."
                )
                print(
                    "Тип ошибки:",
                    type(e).__name__
                )

        else:

            try:

                send_email(
                    smtp,
                    email,
                    doctor_fio,
                    tasks,
                )

                status = make_status(
                    email
                )

                sent_count += 1

                print(
                    "Письмо врачу отправлено."
                )

            except Exception as e:

                print(
                    "Ошибка отправки врачу."
                )
                print(
                    "Тип ошибки:",
                    type(e).__name__
                )

                # --------------------------------------------
                # РЕЗЕРВ
                # --------------------------------------------

                try:

                    send_email(
                        smtp,
                        RESERVE_EMAIL,
                        doctor_fio,
                        tasks,
                    )

                    status = make_reserve_status()

                    reserve_count += 1

                    print(
                        "Отправлено на резервную почту."
                    )

                except Exception as reserve_error:

                    status = make_error_status()

                    error_count += 1

                    print(
                        "Ошибка резервной отправки."
                    )
                    print(
                        "Тип ошибки:",
                        type(reserve_error).__name__
                    )

        # ----------------------------------------------------
        # Обновляем M
        # ----------------------------------------------------

        for row_number in rows:

            updates.append({
                "range": f"M{row_number}",
                "values": [[status]],
            })

    smtp.quit()

    # --------------------------------------------------------
    # BATCH UPDATE
    # --------------------------------------------------------

    if updates:

        tasks_sheet.batch_update(
            updates,
            value_input_option="USER_ENTERED",
        )

    # --------------------------------------------------------
    # ИТОГ
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)

    print(
        "Врачей обработано:",
        len(grouped)
    )

    print(
        "Успешно отправлено врачам:",
        sent_count
    )

    print(
        "Отправлено на резерв:",
        reserve_count
    )

    print(
        "Ошибок:",
        error_count
    )

    print(
        "Строк записано в M:",
        len(updates)
    )

    print(
        "Всего заданий:",
        sum(
            len(item["tasks"])
            for item in grouped.values()
        )
    )

    print(
        "BOTON завершён."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
