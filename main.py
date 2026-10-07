import os
import json
import re
import html
import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# BOTON — версия 1.0 TEST
# Реальный тест отправки только на собственную почту
# Врачи НЕ получают письма
# Колонка M НЕ изменяется
# ============================================================

VERSION = "1.0 TEST"

# ------------------------------------------------------------
# НАСТРОЙКИ GOOGLE SHEETS
# ------------------------------------------------------------

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_DB_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"
COL_DOCTOR_EMAIL = 2

# Резервная почта из исходного бота
DEFAULT_EMAIL = "eagaverdiev@mosmedzdrav.ru"

# ------------------------------------------------------------
# БЕЗОПАСНЫЙ РЕЖИМ
# ------------------------------------------------------------

# True = письмо отправляется только SMTP_USER
# Врачи не получают писем
# Таблица не изменяется
TEST_MODE = True

# Москва — чтобы дата совпадала с логикой Google Apps Script
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ------------------------------------------------------------
# СТОЛБЦЫ ТАБЛИЦЫ
# ------------------------------------------------------------

COL_DATE = 0          # A
COL_BRANCH = 1        # B
COL_DOCTOR = 2        # C
COL_PATIENT_ID = 3    # D
COL_PATIENT_NAME = 4  # E
COL_SOURCE = 5        # F
COL_TASK1 = 6         # G
COL_TASK2 = 8         # I
COL_TASK3 = 10        # K
COL_COMPLETION = 12   # M

# ------------------------------------------------------------
# КЛЮЧЕВЫЕ СЛОВА ФИЛИАЛА 2
# ------------------------------------------------------------

F2_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]


# ============================================================
# GOOGLE
# ============================================================

def connect_google():
    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT"]
    )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )

    return gspread.authorize(credentials)


# ============================================================
# НОРМАЛИЗАЦИЯ ФИО
# ============================================================

def get_signature(name):
    if not name:
        return ""

    clean = str(name).lower()
    clean = clean.replace("ё", "е")

    clean = re.sub(
        r"[^a-zа-яё\s.]",
        " ",
        clean
    )

    clean = re.sub(
        r"\s+",
        " ",
        clean
    ).strip()

    parts = [p for p in clean.split(" ") if p]

    if not parts:
        return ""

    surname = ""
    initials = ""

    # Например:
    # О.В. Нецветаева
    if len(parts) >= 2 and len(parts[0]) <= 2 and "." in parts[0]:
        surname = parts[-1].replace(".", "")

        for part in parts[:-1]:
            part = part.replace(".", "")
            if part:
                initials += part[0]

    # Например:
    # Нецветаева О.В.
    # Нецветаева Ольга Викторовна
    else:
        surname = parts[0].replace(".", "")

        for part in parts[1:]:
            part = part.replace(".", "")
            if part:
                initials += part[0]

    return (surname + " " + initials).strip()


# ============================================================
# БАЗА ВРАЧЕЙ
# ============================================================

def get_doctors_contact_map(client):
    spreadsheet = client.open_by_key(DOCTORS_DB_ID)
    sheet = spreadsheet.worksheet(DOCTORS_SHEET_NAME)

    data = sheet.get_all_values()

    doctors_map = {}

    for row in data[1:]:
        if len(row) <= 2:
            continue

        name = str(row[0]).strip()
        email = str(row[COL_DOCTOR_EMAIL]).strip()

        if not name or not email:
            continue

        signature = get_signature(name)

        if signature:
            doctors_map[signature] = {
                "email": email
            }

    return doctors_map


# ============================================================
# ПОИСК ВРАЧА
# ============================================================

def find_doctor_robust(doctor_name, doctors_map):
    search_sig = get_signature(doctor_name)

    if not search_sig:
        return None

    # 1. Точное совпадение
    if search_sig in doctors_map:
        return doctors_map[search_sig]

    search_parts = search_sig.split(" ")

    search_surname = (
        search_parts[0]
        if search_parts
        else ""
    )

    search_initials = (
        search_parts[1]
        if len(search_parts) > 1
        else ""
    )

    # 2. Фамилия + совместимые инициалы
    for db_key, doctor_data in doctors_map.items():

        db_parts = db_key.split(" ")

        db_surname = (
            db_parts[0]
            if db_parts
            else ""
        )

        db_initials = (
            db_parts[1]
            if len(db_parts) > 1
            else ""
        )

        if db_surname == search_surname:

            if (
                not search_initials
                or not db_initials
                or search_initials.startswith(db_initials)
                or db_initials.startswith(search_initials)
            ):
                return doctor_data

    # 3. Частичное совпадение фамилии
    for db_key, doctor_data in doctors_map.items():

        db_surname = (
            db_key.split(" ")[0]
            if db_key
            else ""
        )

        if (
            len(search_surname) >= 4
            and len(db_surname) >= 4
        ):
            if (
                db_surname in search_surname
                or search_surname in db_surname
            ):
                return doctor_data

    return None


# ============================================================
# SMTP
# ============================================================

def send_email(to_email, subject, html_body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ["SMTP_FROM_EMAIL"]

    message = EmailMessage()

    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject

    message.set_content(
        "BOTON: HTML-письмо. "
        "Если HTML не отображается, откройте письмо в почтовом клиенте."
    )

    message.add_alternative(
        html_body,
        subtype="html"
    )

    with smtplib.SMTP_SSL(
        host,
        port,
        timeout=30
    ) as smtp:

        smtp.login(
            user,
            password
        )

        smtp.send_message(message)


# ============================================================
# HTML ПИСЬМА
# ============================================================

def build_doctor_email(doctor_name, patients, today):
    body = []

    body.append(
        "<html><body>"
    )

    body.append(
        f"<p>Здравствуйте, {html.escape(doctor_name)}!</p>"
    )

    body.append(
        f"<p><b>Задания на {html.escape(today)}</b></p>"
    )

    for patient in patients:

        patient_name = html.escape(
            patient["patient_name"]
        )

        patient_id = html.escape(
            patient["patient_id"]
        )

        body.append(
            "<div style='margin-bottom:20px;'>"
        )

        body.append(
            f"<p><b>Пациент:</b> {patient_name}<br>"
            f"<b>ОМС / ID:</b> {patient_id}</p>"
        )

        body.append(
            "<ul>"
        )

        for task in patient["tasks"]:

            body.append(
                f"<li>{html.escape(task)}</li>"
            )

        body.append(
            "</ul>"
        )

        body.append(
            "</div>"
        )

    body.append(
        "<hr>"
    )

    body.append(
        "<p>"
        "Письмо сформировано автоматически системой BOTON."
        "</p>"
    )

    body.append(
        "</body></html>"
    )

    return "".join(body)


def build_test_email(today, doctors_count, rows_count, tasks_count):
    return f"""
<html>
<body>

<p><b>BOTON — РЕАЛЬНЫЙ ТЕСТ ОТПРАВКИ</b></p>

<p>Тест выполнен успешно.</p>

<table>
<tr>
<td><b>Версия:</b></td>
<td>{html.escape(VERSION)}</td>
</tr>

<tr>
<td><b>Дата:</b></td>
<td>{html.escape(today)}</td>
</tr>

<tr>
<td><b>Врачей найдено:</b></td>
<td>{doctors_count}</td>
</tr>

<tr>
<td><b>Строк с заданиями:</b></td>
<td>{rows_count}</td>
</tr>

<tr>
<td><b>Всего заданий:</b></td>
<td>{tasks_count}</td>
</tr>
</table>

<br>

<p>
Это <b>реальное письмо через SMTP Яндекса</b>.
</p>

<p>
В этом режиме:
</p>

<ul>
<li>врачи не получают письма;</li>
<li>таблица Google Sheets не изменяется;</li>
<li>колонка M не изменяется;</li>
<li>письмо отправляется только на SMTP_USER.</li>
</ul>

<p>
Если это письмо пришло — SMTP и отправка через GitHub Actions работают.
</p>

<p>
BOTON
</p>

</body>
</html>
"""


# ============================================================
# ОСНОВНАЯ ОБРАБОТКА
# ============================================================

def run_tasks_dispatch():

    start_time = datetime.now(MOSCOW_TZ)

    print("=" * 60)
    print(f"BOTON {VERSION}")
    print("РЕЖИМ: ТОЛЬКО ТЕСТ НА СЕБЯ")
    print("=" * 60)

    # --------------------------------------------------------
    # Google
    # --------------------------------------------------------

    client = connect_google()

    tasks_spreadsheet = client.open_by_key(
        TASKS_SPREADSHEET_ID
    )

    tasks_sheet = tasks_spreadsheet.worksheet(
        TASKS_SHEET_NAME
    )

    data = tasks_sheet.get_all_values()

    print("Google Sheets: подключение успешно.")
    print("Строк:", len(data))

    # --------------------------------------------------------
    # База врачей
    # --------------------------------------------------------

    doctors_map = get_doctors_contact_map(client)

    print(
        "Врачей в базе:",
        len(doctors_map)
    )

    # --------------------------------------------------------
    # Дата
    # --------------------------------------------------------

    today = start_time.strftime(
        "%d.%m.%Y"
    )

    print(
        "Дата заданий:",
        today
    )

    # --------------------------------------------------------
    # Поиск заданий
    # --------------------------------------------------------

    doctor_tasks = {}

    processed_rows = 0
    skipped_other_branch = 0
    total_tasks = 0

    for i in range(1, len(data)):

        row = data[i]

        if len(row) < 13:
            continue

        # M уже заполнена
        completion_log = str(
            row[COL_COMPLETION]
        ).strip()

        if (
            completion_log != ""
            and completion_log != "null"
            and completion_log != "undefined"
        ):
            continue

        # Дата
        row_date = str(
            row[COL_DATE]
        ).strip()

        if row_date != today:
            continue

        # Филиал
        branch = str(
            row[COL_BRANCH]
        ).strip().lower()

        is_filial2 = any(
            keyword.lower() in branch
            for keyword in F2_KEYWORDS
        )

        if not is_filial2:
            skipped_other_branch += 1
            continue

        # Врач
        doctor_name = str(
            row[COL_DOCTOR]
        ).strip()

        # Пациент
        patient_name = str(
            row[COL_PATIENT_NAME]
        ).strip()

        patient_id = str(
            row[COL_PATIENT_ID]
        ).strip()

        # Задания
        current_tasks = []

        for column in (
            COL_TASK1,
            COL_TASK2,
            COL_TASK3
        ):

            value = str(
                row[column]
            ).strip()

            if value:
                current_tasks.append(value)

        if not current_tasks:
            continue

        if (
            not doctor_name
            or doctor_name.lower() == "нет данных"
        ):
            doctor_name = "НЕИЗВЕСТНЫЙ ВРАЧ"

        if doctor_name not in doctor_tasks:
            doctor_tasks[doctor_name] = []

        doctor_tasks[doctor_name].append(
            {
                "row_idx": i + 1,
                "patient_name": (
                    patient_name
                    or "Не указано"
                ),
                "patient_id": (
                    patient_id
                    or "Не указан"
                ),
                "tasks": current_tasks,
            }
        )

        processed_rows += 1
        total_tasks += len(current_tasks)

    # --------------------------------------------------------
    # Результат поиска
    # --------------------------------------------------------

    doctors_count = len(
        doctor_tasks
    )

    print(
        "Найдено врачей:",
        doctors_count
    )

    print(
        "Строк с заданиями:",
        processed_rows
    )

    print(
        "Всего заданий:",
        total_tasks
    )

    print(
        "Пропущено других филиалов:",
        skipped_other_branch
    )

    if not doctor_tasks:

        print(
            "Новых заданий на сегодня для Филиала 2 нет."
        )

        return

    # ========================================================
    # БЕЗОПАСНЫЙ ТЕСТ
    # ========================================================

    if TEST_MODE:

        test_email = os.environ[
            "SMTP_USER"
        ]

        print("-" * 60)
        print(
            "TEST_MODE = True"
        )

        print(
            "Отправляется ОДНО тестовое письмо."
        )

        print(
            "Получатель: SMTP_USER"
        )

        print(
            "Врачи: 0 писем"
        )

        print(
            "Изменение Google Sheets: НЕТ"
        )

        test_subject = (
            f"[ТЕСТ] BOTON — проверка отправки "
            f"{today}"
        )

        test_body = build_test_email(
            today=today,
            doctors_count=doctors_count,
            rows_count=processed_rows,
            tasks_count=total_tasks,
        )

        send_email(
            test_email,
            test_subject,
            test_body,
        )

        print(
            "ТЕСТОВОЕ ПИСЬМО УСПЕШНО ОТПРАВЛЕНО."
        )

        print(
            "Колонка M НЕ изменялась."
        )

        print(
            "Письма врачам НЕ отправлялись."
        )

        duration = (
            datetime.now(MOSCOW_TZ)
            - start_time
        ).total_seconds()

        print("-" * 60)
        print(
            f"Время выполнения: {duration:.1f} сек."
        )

        print(
            "BOTON: БЕЗОПАСНЫЙ ТЕСТ ЗАВЕРШЁН."
        )

        return

    # ========================================================
    # РЕАЛЬНАЯ РАССЫЛКА
    # ========================================================

    print("-" * 60)
    print(
        "TEST_MODE = False"
    )

    print(
        "Начинается реальная рассылка врачам."
    )

    doctors_sent = 0
    doctors_failed = 0

    for doctor_name, patients in doctor_tasks.items():

        doctor_data = find_doctor_robust(
            doctor_name,
            doctors_map
        )

        if (
            doctor_data
            and doctor_data.get("email")
            and doctor_data["email"].strip()
        ):
            target_email = doctor_data[
                "email"
            ].strip()

            using_reserve = False

        else:
            target_email = DEFAULT_EMAIL
            using_reserve = True

        subject = (
            f"[Филиал 2] Задания на "
            f"{today} - {doctor_name}"
        )

        email_body = build_doctor_email(
            doctor_name,
            patients,
            today,
        )

        try:

            send_email(
                target_email,
                subject,
                email_body,
            )

            doctors_sent += 1

            print(
                "Письмо отправлено."
            )

            # Здесь будет запись в M.
            # В тестовом режиме этот блок
            # недостижим.

        except Exception as error:

            print(
                "Ошибка отправки письма."
            )

            # Если основная почта не работает,
            # пробуем резервную.
            if not using_reserve:

                try:

                    send_email(
                        DEFAULT_EMAIL,
                        subject,
                        email_body,
                    )

                    doctors_sent += 1

                    print(
                        "Письмо отправлено на резервную почту."
                    )

                except Exception:

                    doctors_failed += 1

                    print(
                        "Резервная отправка также не удалась."
                    )

            else:

                doctors_failed += 1

    duration = (
        datetime.now(MOSCOW_TZ)
        - start_time
    ).total_seconds()

    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)

    print(
        "Врачей:",
        doctors_count
    )

    print(
        "Отправлено:",
        doctors_sent
    )

    print(
        "Ошибок:",
        doctors_failed
    )

    print(
        "Строк:",
        processed_rows
    )

    print(
        "Заданий:",
        total_tasks
    )

    print(
        f"Время выполнения: {duration:.1f} сек."
    )

    print(
        "BOTON: РАССЫЛКА ЗАВЕРШЕНА."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    run_tasks_dispatch()
