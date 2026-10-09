
"""Настройки конкретной медицинской организации."""

CLINIC_NAME = "ГП № 107"
CLINIC_CODE = "gp107"

# Таблица с заданиями
TASKS_SPREADSHEET_ID = "ID_ТАБЛИЦЫ_ЗАДАНИЙ"
TASKS_SHEET_NAME = "Лист1"

# База врачей
DOCTORS_SPREADSHEET_ID = "ID_ТАБЛИЦЫ_ВРАЧЕЙ"
DOCTORS_SHEET_NAME = "Врачи"

# Журнал бота
LOG_SHEET_NAME = "BoTon_Log"

# Номера столбцов: индексация с нуля
COLUMNS = {
    "date": 0,
    "branch": 1,
    "doctor": 2,
    "patient_id": 3,
    "patient_name": 4,
    "source": 5,
    "task1": 6,
    "status1": 7,
    "task2": 8,
    "status2": 9,
    "task3": 10,
    "status3": 11,
    "completion": 12,
}

# Допустимые обозначения филиала
BRANCH_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]

# Названия заданий
TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}

TASK_LETTERS = ["А", "Б", "В"]
