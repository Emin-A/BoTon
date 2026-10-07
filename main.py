import os
import json
import re
import html
import smtplib
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# BOTON — версия 1.1
# ============================================================

VERSION = "1.1"

# True  = тест: письмо только себе, таблица не изменяется
# False = реальная рассылка врачам + запись результата в M
TEST_MODE = True

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


# ============================================================
# GOOGLE SHEETS
# ============================================================

TASKS_SPREADSHEET_ID = (
    "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
)
TASKS_SHEET_NAME = "Лист1"

DOCTORS_DB_ID = (
    "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
)
DOCTORS_SHEET_NAME = "Врачи"

COL_DOCTOR_EMAIL = 2

DEFAULT_EMAIL = "eagaverdiev@mosmedzdrav.ru"


# ============================================================
# СТОЛБЦЫ ТАБЛИЦЫ ЗАДАНИЙ
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


# ============================================================
# ФИЛИАЛ 2
# ============================================================

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
# GOOGLE CONNECTION
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
        clean,
    )

    clean = re.sub(
        r"\s+",
        " ",
        clean,
    ).strip()

    parts = [
        p for p in clean.split(" ")
        if p
    ]

    if not parts:
        return ""

    surname = ""
    initials = ""

    if (
        len(parts) >= 2
        and len(parts[0]) <= 2
        and "." in parts[0]
    ):
        surname = parts[-1].replace(".", "")

        for part in parts[:-1]:
            part = part.replace(".", "")

            if part:
                initials += part[0]

    else:
        surname = parts[0].replace(".", "")

        for part in parts[1:]:
            part = part.replace(".", "")

            if part:
                initials += part[0]

    return (
        surname + " " + initials
    ).strip()


# ============================================================
# БАЗА ВРАЧЕЙ
# ============================================================

def get_doctors_contact_map(client):
    spreadsheet = client.open_by_key(
        DOCTORS_DB_ID
    )

    sheet = spreadsheet.worksheet(
        DOCTORS_SHEET_NAME
    )

    data = sheet.get_all_values()

    doctors_map = {}

    for row in data[1:]:

        if len(row) <= COL_DOCTOR_EMAIL:
            continue

        name = str(row[0]).strip()
        email = str(
            row[COL_DOCTOR_EMAIL]
        ).strip()

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

def find_doctor_robust(
    doctor_name,
    doctors_map,
):
    search_sig = get_signature(
        doctor_name
    )

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
                or search_initials.startswith(
                    db_initials
                )
                or db_initials.startswith(
                    search_initials
                )
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
            and (
                db_surname in search_surname
                or search_surname in db_surname
            )
        ):
            return doctor_data

    return None


# ============================================================
# SMTP
# ============================================================

def send_email(
    to_email,
    subject,
    html_body,
):
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
        "BOTON: HTML-письмо."
    )

    message.add_alternative(
        html_body,
        subtype="html",
    )

    with smtplib.SMTP_SSL(
        host,
        port,
        timeout=30,
    ) as smtp:

        smtp.login(
            user,
            password,
        )

        smtp.send_message(
            message
        )


# ============================================================
# HTML ПИСЬМА
# ============================================================

def build_doctor_email(
    doctor_name,
    patients,
    today,
):
    body = [
        "<html><body>",
        f"<p>Здравствуйте, "
        f"{html.escape(doctor_name)}!</p>",
        f"<p><b>Задания на "
        f"{html.escape(today)}</b></p>",
    ]

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
            f"<p><b>Пациент:</b> "
            f"{patient_name}<br>"
            f"<b>ОМС / ID:</b> "
            f"{patient_id}</p>"
        )

        body.append("<ul>")

        for task in patient["tasks"]:

            body.append(
                f"<li>{html.escape(task)}</li>"
            )

        body.append("</ul>")
        body.append("</div>")

    body.extend([
        "<hr>",
        "<p>"
        "Письмо сформировано автоматически "
        "системой BOTON."
        "</p>",
        "</body></html>",
    ])

    return "".join(body)


def build_test_email(
    today,
    doctors_count,
    rows_count,
    tasks_count,
):
    return f"""
<html>
<body>

<p><b>BOTON — ТЕСТ ВЕРСИИ 1.1</b></p>

<p>
Реальная проверка SMTP-отправки.
</p>

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
Проверено:
</p>

<ul>
<li>Google Sheets — OK</li>
<li>SMTP Яндекса — OK</li>
<li>реальная отправка — OK</li>
<li>врачи — 0 писем</li>
<li>колонка M — без изменений</li>
</ul>

<p>
Это тестовый режим BOTON.
</p>

</body>
</html>
"""


# ============================================================
# ФОРМИРОВАНИЕ СТАТУСА
# ============================================================

def make_success_status(
    today,
    target_email,
    reserve=False,
):
    time_now = datetime.now(
        MOSCOW_TZ
    ).strftime("%H:%M:%S")

    if reserve:
        return (
            f"ОТПРАВЛЕНО НА РЕЗЕРВ "
            f"{today} {time_now}"
        )

    return (
        f"ОТПРАВЛЕНО "
        f"{today} {time_now}"
    )


def make_error_status(
    today,
):
    time_now = datetime.now(
        MOSCOW_TZ
    ).strftime("%H:%M:%S")

    return (
        f"ОШИБКА ОТПРАВКИ "
        f"{today} {time_now}"
    )


# ============================================================
# ЗАПИСЬ В КОЛОНКУ M
# ============================================================

def write_statuses(
    tasks_sheet,
    status_updates,
):
    if not status_updates:
        return

    updates = []

    for row_idx, status in status_updates.items():

        updates.append({
            "range": f"M{row_idx}",
            "values": [[status]],
        })

    tasks_sheet.batch_update(
        updates,
        value_input_option="USER_ENTERED",
    )

    print(
        "Записей в колонку M:",
        len(updates),
    )


# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================

def run_tasks_dispatch():

    start_time = datetime.now(
        MOSCOW_TZ
    )

    print("=" * 60)
    print(
        f"BOTON {VERSION}"
    )
    print(
        f"TEST_MODE = {TEST_MODE}"
    )
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

    print(
        "Google Sheets: подключение успешно."
    )

    print(
        "Строк:",
        len(data),
    )

    # --------------------------------------------------------
    # Врачи
    # --------------------------------------------------------

    doctors_map = get_doctors_contact_map(
        client
    )

    print(
        "Врачей в базе:",
        len(doctors_map),
    )

    # --------------------------------------------------------
    # Дата
    # --------------------------------------------------------

    today = start_time.strftime(
        "%d.%m.%Y"
    )

    print(
        "Дата заданий:",
        today,
    )

    # --------------------------------------------------------
    # Сбор заданий
    # --------------------------------------------------------

    doctor_tasks = {}

    processed_rows = 0
    skipped_other_branch = 0
    total_tasks = 0

    for i in range(
        1,
        len(data),
    ):

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

        if (
            not doctor_name
            or doctor_name.lower()
            == "нет данных"
        ):
            doctor_name = "НЕИЗВЕСТНЫЙ ВРАЧ"

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
            COL_TASK3,
        ):

            value = str(
                row[column]
            ).strip()

            if value:
                current_tasks.append(
                    value
                )

        if not current_tasks:
            continue

        if doctor_name not in doctor_tasks:
            doctor_tasks[doctor_name] = []

        doctor_tasks[doctor_name].append({
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
        })

        processed_rows += 1
        total_tasks += len(
            current_tasks
        )

    # --------------------------------------------------------
    # Статистика
    # --------------------------------------------------------

    doctors_count = len(
        doctor_tasks
    )

    print(
        "Найдено врачей:",
        doctors_count,
    )

    print(
        "Строк с заданиями:",
        processed_rows,
    )

    print(
        "Всего заданий:",
        total_tasks,
    )

    print(
        "Пропущено других филиалов:",
        skipped_other_branch,
    )

    if not doctor_tasks:

        print(
            "Новых заданий нет."
        )

        return

    # ========================================================
    # TEST MODE
    # ========================================================

    if TEST_MODE:

        test_email = os.environ[
            "SMTP_USER"
        ]

        print("-" * 60)
        print(
            "БЕЗОПАСНЫЙ ТЕСТ"
        )

        print(
            "Получатель: SMTP_USER"
        )

        print(
            "Врачи: 0 писем"
        )

        print(
            "Изменение M: НЕТ"
        )

        test_subject = (
            f"[ТЕСТ] BOTON v{VERSION} "
            f"— {today}"
        )

        test_body = build_test_email(
            today,
            doctors_count,
            processed_rows,
            total_tasks,
        )

        send_email(
            test_email,
            test_subject,
            test_body,
        )

        print(
            "Тестовое письмо успешно отправлено."
        )

        print(
            "Колонка M НЕ изменялась."
        )

        duration = (
            datetime.now(MOSCOW_TZ)
            - start_time
        ).total_seconds()

        print(
            f"Время выполнения: "
            f"{duration:.1f} сек."
        )

        print(
            "BOTON: ТЕСТ 1.1 ЗАВЕРШЁН."
        )

        return

    # ========================================================
    # REAL MODE
    # ========================================================

    print("-" * 60)
    print(
        "РЕАЛЬНАЯ РАССЫЛКА"
    )

    status_updates = {}

    doctors_sent = 0
    doctors_failed = 0
    reserve_sent = 0

    for doctor_name, patients in doctor_tasks.items():

        doctor_data = find_doctor_robust(
            doctor_name,
            doctors_map,
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

        # ----------------------------------------------------
        # Основная почта
        # ----------------------------------------------------

        try:

            send_email(
                target_email,
                subject,
                email_body,
            )

            doctors_sent += 1

            status = make_success_status(
                today,
                target_email,
                reserve=using_reserve,
            )

            for patient in patients:
                status_updates[
                    patient["row_idx"]
                ] = status

            if using_reserve:
                reserve_sent += 1

            print(
                "Отправлено: 1"
            )

        except Exception as error:

            print(
                "Основная отправка не удалась."
            )

            # ------------------------------------------------
            # Резервная почта
            # ------------------------------------------------

            if not using_reserve:

                try:

                    send_email(
                        DEFAULT_EMAIL,
                        subject,
                        email_body,
                    )

                    doctors_sent += 1
                    reserve_sent += 1

                    status = make_success_status(
                        today,
                        DEFAULT_EMAIL,
                        reserve=True,
                    )

                    for patient in patients:
                        status_updates[
                            patient["row_idx"]
                        ] = status

                    print(
                        "Резервная отправка успешна."
                    )

                except Exception:

                    doctors_failed += 1

                    status = make_error_status(
                        today
                    )

                    for patient in patients:
                        status_updates[
                            patient["row_idx"]
                        ] = status

                    print(
                        "Основная и резервная "
                        "отправка не удались."
                    )

            else:

                doctors_failed += 1

                status = make_error_status(
                    today
                )

                for patient in patients:
                    status_updates[
                        patient["row_idx"]
                    ] = status

                print(
                    "Резервная отправка не удалась."
                )

        # Небольшая пауза между врачами
        time.sleep(0.5)

    # ========================================================
    # ЗАПИСЬ В M
    # ========================================================

    if status_updates:

        write_statuses(
            tasks_sheet,
            status_updates,
        )

    # ========================================================
    # ИТОГ
    # ========================================================

    duration = (
        datetime.now(MOSCOW_TZ)
        - start_time
    ).total_seconds()

    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)

    print(
        "Врачей найдено:",
        doctors_count,
    )

    print(
        "Успешно отправлено:",
        doctors_sent,
    )

    print(
        "Через резерв:",
        reserve_sent,
    )

    print(
        "Ошибок:",
        doctors_failed,
    )

    print(
        "Строк с заданиями:",
        processed_rows,
    )

    print(
        "Всего заданий:",
        total_tasks,
    )

    print(
        "Записано статусов в M:",
        len(status_updates),
    )

    print(
        f"Время выполнения: "
        f"{duration:.1f} сек."
    )

    print(
        "BOTON: РАССЫЛКА ЗАВЕРШЕНА."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    run_tasks_dispatch()
