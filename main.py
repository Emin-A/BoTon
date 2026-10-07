import os
import json
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"

DOCTORS_DB_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

# C в таблице врачей
COL_DOCTOR_EMAIL = 2

# Резервная почта
DEFAULT_EMAIL = "eagaverdiev@mosmedzdrav.ru"

# Тестовый режим.
# На этом этапе отправки всё равно нет.
TEST_MODE = False

# Индексы колонок, 0-based
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


# Фильтр Филиала 2
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
# GOOGLE SHEETS
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
# Аналог getSignature() из Apps Script
# ============================================================

def get_signature(name):
    if not name:
        return ""

    clean = str(name).lower()

    # ё -> е
    clean = clean.replace("ё", "е")

    # Оставляем буквы, пробелы и точки
    clean = re.sub(r"[^a-zа-яё\s.]", " ", clean)

    # Убираем лишние пробелы
    clean = re.sub(r"\s+", " ", clean).strip()

    parts = [p for p in clean.split(" ") if p]

    if not parts:
        return ""

    surname = ""
    initials = ""

    # Формат:
    # О.В. Нецветаева
    if len(parts) >= 2 and len(parts[0]) <= 2 and "." in parts[0]:
        surname = parts[-1].replace(".", "")

        for part in parts[:-1]:
            part = part.replace(".", "")
            if part:
                initials += part[0]

    # Формат:
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
# ЗАГРУЗКА БАЗЫ ВРАЧЕЙ
# ============================================================

def get_doctors_contact_map(client):
    spreadsheet = client.open_by_key(DOCTORS_DB_ID)
    sheet = spreadsheet.worksheet(DOCTORS_SHEET_NAME)

    data = sheet.get_all_values()

    doctors_map = {}

    for row in data[1:]:
        if len(row) <= COL_DOCTOR_EMAIL:
            continue

        name = str(row[0]).strip()
        email = str(row[COL_DOCTOR_EMAIL]).strip()

        if name and email:
            signature = get_signature(name)

            if signature:
                doctors_map[signature] = {
                    "email": email
                }

    return doctors_map


# ============================================================
# ПОИСК ВРАЧА
# Аналог findChatIdRobust()
# ============================================================

def find_doctor_robust(doctor_name, doctors_map):
    search_sig = get_signature(doctor_name)

    if not search_sig:
        return None

    # --------------------------------------------------------
    # 1. Точное совпадение
    # --------------------------------------------------------

    if search_sig in doctors_map:
        print(
            f'  Точное совпадение: "{doctor_name}" '
            f'-> "{search_sig}"'
        )

        return doctors_map[search_sig]

    search_parts = search_sig.split(" ")

    search_surname = search_parts[0] if search_parts else ""
    search_initials = search_parts[1] if len(search_parts) > 1 else ""

    # --------------------------------------------------------
    # 2. Фамилия + инициалы
    # --------------------------------------------------------

    for db_key, doctor_data in doctors_map.items():

        db_parts = db_key.split(" ")

        db_surname = db_parts[0] if db_parts else ""
        db_initials = db_parts[1] if len(db_parts) > 1 else ""

        if db_surname == search_surname:

            if (
                not search_initials
                or not db_initials
                or search_initials.startswith(db_initials)
                or db_initials.startswith(search_initials)
            ):
                print(
                    f'  Совпадение по фамилии: "{doctor_name}" '
                    f'-> "{db_key}"'
                )

                return doctor_data

    # --------------------------------------------------------
    # 3. Частичное совпадение фамилии
    # --------------------------------------------------------

    for db_key, doctor_data in doctors_map.items():

        db_surname = db_key.split(" ")[0] if db_key else ""

        if len(search_surname) >= 4 and len(db_surname) >= 4:

            if (
                db_surname in search_surname
                or search_surname in db_surname
            ):
                print(
                    f'  Частичное совпадение: "{doctor_name}" '
                    f'-> "{db_key}"'
                )

                return doctor_data

    print(
        f'  Врач не найден: "{doctor_name}" '
        f'(подпись: "{search_sig}")'
    )

    return None


# ============================================================
# ОСНОВНАЯ ОБРАБОТКА
# ============================================================

def run_tasks_dispatch():

    start_time = datetime.now()

    print()
    print("=" * 60)
    print("BOTON: НАЧАЛО ОБРАБОТКИ ЗАДАНИЙ")
    print("=" * 60)

    client = connect_google()

    # --------------------------------------------------------
    # Таблица задач
    # --------------------------------------------------------

    tasks_spreadsheet = client.open_by_key(
        TASKS_SPREADSHEET_ID
    )

    tasks_sheet = tasks_spreadsheet.worksheet(
        TASKS_SHEET_NAME
    )

    data = tasks_sheet.get_all_values()

    print()
    print("Таблица задач:", tasks_spreadsheet.title)
    print("Лист:", tasks_sheet.title)
    print("Строк:", len(data))
    print(
        "Столбцов:",
        len(data[0]) if data else 0
    )

    # --------------------------------------------------------
    # База врачей
    # --------------------------------------------------------

    doctors_map = get_doctors_contact_map(client)

    print()
    print("Врачей в базе:", len(doctors_map))

    # --------------------------------------------------------
    # Сегодняшняя дата
    # --------------------------------------------------------

    today = datetime.now().strftime("%d.%m.%Y")

    now = datetime.now().strftime(
        "%d.%m.%Y %H:%M"
    )

    print()
    print("Сегодня:", today)

    # --------------------------------------------------------
    # Статистика
    # --------------------------------------------------------

    stats = {
        "processed_rows": 0,
        "doctors_found": 0,
        "doctors_not_found": 0,
        "skipped": 0,
        "tasks": 0,
    }

    # --------------------------------------------------------
    # Группировка заданий по врачам
    # --------------------------------------------------------

    doctor_tasks = {}

    # Пропускаем заголовок
    for i in range(1, len(data)):

        row = data[i]

        row_idx = i + 1

        # ----------------------------------------------------
        # Проверяем размер строки
        # ----------------------------------------------------

        if len(row) < 13:
            continue

        # ----------------------------------------------------
        # Уже обработано?
        # Колонка M
        # ----------------------------------------------------

        completion_log = str(
            row[COL_COMPLETION]
        ).strip()

        if (
            completion_log != ""
            and completion_log != "null"
            and completion_log != "undefined"
        ):
            continue

        # ----------------------------------------------------
        # Дата
        # ----------------------------------------------------

        row_date = str(row[COL_DATE]).strip()

        if row_date != today:
            continue

        # ----------------------------------------------------
        # Филиал 2
        # ----------------------------------------------------

        branch = str(
            row[COL_BRANCH]
        ).strip().lower()

        is_filial2 = any(
            keyword.lower() in branch
            for keyword in F2_KEYWORDS
        )

        if not is_filial2:
            stats["skipped"] += 1
            continue

        # ----------------------------------------------------
        # Врач
        # ----------------------------------------------------

        doctor_name = str(
            row[COL_DOCTOR]
        ).strip()

        patient_name = str(
            row[COL_PATIENT_NAME]
        ).strip()

        patient_id = str(
            row[COL_PATIENT_ID]
        ).strip()

        # ----------------------------------------------------
        # Все задания G / I / K
        # ----------------------------------------------------

        current_tasks = []

        if str(row[COL_TASK1]).strip():
            current_tasks.append(
                str(row[COL_TASK1]).strip()
            )

        if str(row[COL_TASK2]).strip():
            current_tasks.append(
                str(row[COL_TASK2]).strip()
            )

        if str(row[COL_TASK3]).strip():
            current_tasks.append(
                str(row[COL_TASK3]).strip()
            )

        if not current_tasks:
            continue

        if (
            not doctor_name
            or doctor_name.lower() == "нет данных"
        ):
            doctor_name = "НЕИЗВЕСТНЫЙ ВРАЧ"

        if doctor_name not in doctor_tasks:
            doctor_tasks[doctor_name] = []

        doctor_tasks[doctor_name].append({
            "row_idx": row_idx,
            "patient_name": patient_name or "Не указано",
            "patient_id": patient_id or "Не указан",
            "tasks": current_tasks,
        })

        stats["processed_rows"] += 1
        stats["tasks"] += len(current_tasks)

    # --------------------------------------------------------
    # Нет заданий
    # --------------------------------------------------------

    if not doctor_tasks:

        print()
        print(
            "Нет новых заданий на сегодня "
            "для Филиала 2."
        )

        print(
            "Пропущено других филиалов:",
            stats["skipped"]
        )

        return

    # --------------------------------------------------------
    # Результат
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print(
        f"НАЙДЕНО ВРАЧЕЙ: {len(doctor_tasks)}"
    )
    print("=" * 60)

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
            target_email = doctor_data["email"].strip()
            found = True
            stats["doctors_found"] += 1

        else:
            target_email = DEFAULT_EMAIL
            found = False
            stats["doctors_not_found"] += 1

        total_tasks = sum(
            len(patient["tasks"])
            for patient in patients
        )

        print()
        print("-" * 60)
        print("Врач:", doctor_name)
        print("Пациентов:", len(patients))
        print("Заданий:", total_tasks)
        print(
            "Email:",
            target_email if found else "РЕЗЕРВНАЯ ПОЧТА"
        )

        for patient in patients:
            print(
                f"  Пациент: {patient['patient_name']} "
                f"| ID: {patient['patient_id']}"
            )

            for task in patient["tasks"]:
                print(
                    f"    - {task}"
                )

    # --------------------------------------------------------
    # Итог
    # --------------------------------------------------------

    duration = (
        datetime.now() - start_time
    ).total_seconds()

    print()
    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)

    print(
        "Дата заданий:",
        today
    )

    print(
        "Врачей с найденной почтой:",
        stats["doctors_found"]
    )

    print(
        "Врачей без найденной почты:",
        stats["doctors_not_found"]
    )

    print(
        "Строк с заданиями:",
        stats["processed_rows"]
    )

    print(
        "Всего заданий:",
        stats["tasks"]
    )

    print(
        "Пропущено других филиалов:",
        stats["skipped"]
    )

    print(
        f"Время выполнения: {duration:.1f} сек."
    )

    print()
    print(
        "ВАЖНО: письма НЕ отправлялись, "
        "таблица НЕ изменялась."
    )

    print()
    print(
        "BOTON: тест обработки успешно завершён!"
    )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    run_tasks_dispatch()
