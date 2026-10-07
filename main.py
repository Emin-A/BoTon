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
# BOTON v1.2
# ============================================================

VERSION = "1.2"

# ВАЖНО:
# False = реальная рассылка
# True  = тест только на SMTP_USER
TEST_MODE = False

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
# СТОЛБЦЫ ЗАДАНИЙ
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
# GOOGLE
# ============================================================

def connect_google():
    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT"]
    )

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )

    return gspread.authorize(credentials)


# ============================================================
# ФИО → ФАМИЛИЯ + ИНИЦИАЛЫ
# ============================================================

def get_signature(name):
    if not name:
        return ""

    clean = str(name).lower().replace("ё", "е")
    clean = re.sub(r"[^a-zа-яё\s.]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    parts = [p for p in clean.split() if p]

    if not parts:
        return ""

    if (
        len(parts) >= 2
        and len(parts[0]) <= 2
        and "." in parts[0]
    ):
        surname = parts[-1].replace(".", "")
        initials = ""

        for part in parts[:-1]:
            part = part.replace(".", "")
            if part:
                initials += part[0]

    else:
        surname = parts[0].replace(".", "")
        initials = ""

        for part in parts[1:]:
            part = part.replace(".", "")
            if part:
                initials += part[0]

    return f"{surname} {initials}".strip()


# ============================================================
# БАЗА ВРАЧЕЙ
# ============================================================

def get_doctors_contact_map(client):
    sheet = client.open_by_key(
        DOCTORS_DB_ID
    ).worksheet(
        DOCTORS_SHEET_NAME
    )

    doctors_map = {}

    for row in sheet.get_all_values()[1:]:
        if len(row) <= COL_DOCTOR_EMAIL:
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

    parts = search_sig.split()

    surname = parts[0] if parts else ""
    initials = parts[1] if len(parts) > 1 else ""

    # 2. Фамилия + совместимые инициалы
    for key, data in doctors_map.items():

        db_parts = key.split()

        db_surname = db_parts[0] if db_parts else ""
        db_initials = (
            db_parts[1]
            if len(db_parts) > 1
            else ""
        )

        if db_surname == surname:
            if (
                not initials
                or not db_initials
                or initials.startswith(db_initials)
                or db_initials.startswith(initials)
            ):
                return data

    # 3. Частичное совпадение фамилии
    for key, data in doctors_map.items():

        db_surname = key.split()[0] if key else ""

        if (
            len(surname) >= 4
            and len(db_surname) >= 4
            and (
                db_surname in surname
                or surname in db_surname
            )
        ):
            return data

    return None


# ============================================================
# SMTP
# ============================================================

def smtp_connect():
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    smtp = smtplib.SMTP_SSL(
        host,
        port,
        timeout=30,
    )

    smtp.login(
        user,
        password,
    )

    return smtp


def send_email(
    smtp,
    to_email,
    subject,
    html_body,
):
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

    smtp.send_message(message)


# ============================================================
# HTML ПИСЬМО ВРАЧУ
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

        body.append(
            "<div style='margin-bottom:20px;'>"
        )

        body.append(
            f"<p><b>Пациент:</b> "
            f"{html.escape(patient['patient_name'])}"
            f"<br>"
            f"<b>ОМС / ID:</b> "
            f"{html.escape(patient['patient_id'])}"
            f"</p>"
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


# ============================================================
# ТЕСТОВОЕ ПИСЬМО
# ============================================================

def build_test_email(
    today,
    doctors_count,
    rows_count,
    tasks_count,
):
    return f"""
<html>
<body>

<p><b>BOTON v{VERSION} — ТЕСТ</b></p>

<p>Реальный тест SMTP.</p>

<table>
<tr>
<td><b>Дата:</b></td>
<td>{html.escape(today)}</td>
</tr>

<tr>
<td><b>Врачей:</b></td>
<td>{doctors_count}</td>
</tr>

<tr>
<td><b>Строк:</b></td>
<td>{rows_count}</td>
</tr>

<tr>
<td><b>Заданий:</b></td>
<td>{tasks_count}</td>
</tr>
</table>

<p>
Врачи не получают письма.
Колонка M не изменяется.
</p>

</body>
</html>
"""


# ============================================================
# СТАТУС ДЛЯ M
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
# ЗАПИСЬ M
# ============================================================

def write_statuses(
    tasks_sheet,
    status_updates,
):
    if not status_updates:
        return

    updates = [
        {
            "range": f"M{row_idx}",
            "values": [[status]],
        }
        for row_idx, status
        in status_updates.items()
    ]

    tasks_sheet.batch_update(
        updates,
        value_input_option="USER_ENTERED",
    )


# ============================================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================================

def run_tasks_dispatch():

    started = datetime.now(
        MOSCOW_TZ
    )

    print("=" * 60)
    print(
        f"BOTON v{VERSION}"
    )
    print(
        f"TEST_MODE = {TEST_MODE}"
    )
    print("=" * 60)

    # --------------------------------------------------------
    # Google
    # --------------------------------------------------------

    client = connect_google()

    tasks_sheet = client.open_by_key(
        TASKS_SPREADSHEET_ID
    ).worksheet(
        TASKS_SHEET_NAME
    )

    data = tasks_sheet.get_all_values()

    print(
        "Google Sheets: OK"
    )

  print(
    "Строк:",
    len(data),
)
    # --------------------------------------------------------
    # Дата Москва
    # --------------------------------------------------------

    today = started.strftime(
        "%d.%m.%Y"
    )

    print(
        "Дата:",
        today,
    )

    # --------------------------------------------------------
    # Сначала фильтруем таблицу.
    # Базу врачей читаем только если
    # реально найдены новые задания.
    # --------------------------------------------------------

    doctor_tasks = {}

    skipped = 0
    processed_rows = 0
    total_tasks = 0

    for i, row in enumerate(
        data[1:],
        start=2,
    ):

        if len(row) < 13:
            continue

        # M заполнена → уже отправлено
        completion = str(
            row[COL_COMPLETION]
        ).strip()

        if (
            completion
            and completion.lower()
            not in ("null", "undefined")
        ):
            continue

        # Дата
        if (
            str(row[COL_DATE]).strip()
            != today
        ):
            continue

        # Филиал 2
        branch = str(
            row[COL_BRANCH]
        ).strip().lower()

        if not any(
            keyword.lower() in branch
            for keyword in F2_KEYWORDS
        ):
            skipped += 1
            continue

        doctor_name = str(
            row[COL_DOCTOR]
        ).strip()

        if (
            not doctor_name
            or doctor_name.lower()
            == "нет данных"
        ):
            doctor_name = "НЕИЗВЕСТНЫЙ ВРАЧ"

        patient_name = str(
            row[COL_PATIENT_NAME]
        ).strip()

        patient_id = str(
            row[COL_PATIENT_ID]
        ).strip()

        tasks = []

        for col in (
            COL_TASK1,
            COL_TASK2,
            COL_TASK3,
        ):
            value = str(
                row[col]
            ).strip()

            if value:
                tasks.append(value)

        if not tasks:
            continue

        doctor_tasks.setdefault(
            doctor_name,
            []
        ).append({
            "row_idx": i,
            "patient_name": (
                patient_name
                or "Не указано"
            ),
            "patient_id": (
                patient_id
                or "Не указан"
            ),
            "tasks": tasks,
        })

        processed_rows += 1
        total_tasks += len(tasks)

    doctors_count = len(
        doctor_tasks
    )

    print(
        "Новых врачей:",
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
        "Других филиалов:",
        skipped,
    )

    # --------------------------------------------------------
    # Нет новых данных
    # --------------------------------------------------------

    if not doctor_tasks:

        print(
            "Новых заданий нет."
        )

        print(
            "Следующая проверка — по расписанию GitHub Actions."
        )

        return

    # --------------------------------------------------------
    # TEST MODE
    # --------------------------------------------------------

    if TEST_MODE:

        print(
            "TEST_MODE: отправляем только себе."
        )

        smtp = smtp_connect()

        try:
            send_email(
                smtp,
                os.environ["SMTP_USER"],
                f"[ТЕСТ] BOTON v{VERSION} — {today}",
                build_test_email(
                    today,
                    doctors_count,
                    processed_rows,
                    total_tasks,
                ),
            )
        finally:
            smtp.quit()

        print(
            "Тестовое письмо отправлено."
        )

        print(
            "M не изменялась."
        )

        return

    # ========================================================
    # РЕАЛЬНАЯ РАССЫЛКА
    # ========================================================

    doctors_map = get_doctors_contact_map(
        client
    )

    print(
        "Врачей в базе:",
        len(doctors_map),
    )

    smtp = smtp_connect()

    status_updates = {}

    sent = 0
    reserve_sent = 0
    failed = 0

    try:

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

                reserve = False

            else:
                target_email = DEFAULT_EMAIL
                reserve = True

            subject = (
                f"[Филиал 2] Задания на "
                f"{today} - {doctor_name}"
            )

            body = build_doctor_email(
                doctor_name,
                patients,
                today,
            )

            # ------------------------------------------------
            # Основной адрес
            # ------------------------------------------------

            try:

                send_email(
                    smtp,
                    target_email,
                    subject,
                    body,
                )

                status = (
                    make_reserve_status()
                    if reserve
                    else make_status(
                        target_email
                    )
                )

                sent += 1

                if reserve:
                    reserve_sent += 1

            # ------------------------------------------------
            # Основной адрес не сработал
            # ------------------------------------------------

            except Exception:

                if not reserve:

                    try:

                        send_email(
                            smtp,
                            DEFAULT_EMAIL,
                            subject,
                            body,
                        )

                        status = make_reserve_status()

                        sent += 1
                        reserve_sent += 1

                    except Exception:

                        status = make_error_status()
                        failed += 1

                else:

                    status = make_error_status()
                    failed += 1

            # ------------------------------------------------
            # Статус каждой строки пациента
            # ------------------------------------------------

            for patient in patients:
                status_updates[
                    patient["row_idx"]
                ] = status

        # ----------------------------------------------------
        # Одна batch-запись в M
        # ----------------------------------------------------

        write_statuses(
            tasks_sheet,
            status_updates,
        )

    finally:
        smtp.quit()

    elapsed = (
        datetime.now(MOSCOW_TZ)
        - started
    ).total_seconds()

    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)

    print(
        "Врачей:",
        doctors_count,
    )

    print(
        "Успешно отправлено:",
        sent,
    )

    print(
        "Через резерв:",
        reserve_sent,
    )

    print(
        "Ошибок:",
        failed,
    )

    print(
        "Строк обработано:",
        processed_rows,
    )

    print(
        "Заданий:",
        total_tasks,
    )

    print(
        "Статусов записано в M:",
        len(status_updates),
    )

    print(
        f"Время: {elapsed:.1f} сек."
    )

    print(
        "BOTON v1.2: завершено."
    )


if __name__ == "__main__":
    run_tasks_dispatch()
