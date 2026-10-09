"""Централизованные настройки BoTon. Секреты хранятся только в GitHub Secrets."""
import os
from datetime import time
from zoneinfo import ZoneInfo

VERSION = "3.3"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _env_flag(name):
    """True, только если переменная окружения равна 'true'."""
    return os.environ.get(name, "").strip().lower() == "true"


# Режимы запуска теперь включаются кнопкой в GitHub (Run workflow),
# а не правкой кода. При обычном запуске по расписанию все равны False.
FORCE_RESEND = _env_flag("FORCE_RESEND")
TEST_TO_RESERVE = _env_flag("TEST_TO_RESERVE")
TEST_RECEIVE_ONLY = _env_flag("TEST_RECEIVE_ONLY")
TEST_DOCTOR_DIAG = False
TEST_EMAIL = "agaverdiev@mail.ru"
REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"
DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"
LOG_SHEET_NAME = "BoTon_Log"

COL_DATE = 0
COL_BRANCH = 1
COL_DOCTOR = 2
COL_PATIENT_ID = 3
COL_PATIENT_NAME = 4
COL_SOURCE = 5
COL_TASK1 = 6
COL_STATUS1 = 7
COL_TASK2 = 8
COL_STATUS2 = 9
COL_TASK3 = 10
COL_STATUS3 = 11
COL_COMPLETION = 12

# Приоритет статусов: более высокий статус не перезаписывается более низким.
# Порядок выбран как разумное предположение — при необходимости измените числа.
STATUS_PRIORITY = {
    "": 0,
    "❌ Не выполнено": 1,
    "⚠️ Частично": 2,
    "✅ Выполнено": 3,
}

BRANCH_KEYWORDS = [
    "ф2", "филиал 2", "филиал № 2", "гп 107 ф 2",
    "гп 48", "107 ф 2", "гп 107 ф2",
]
TASK_LETTERS = ["А", "Б", "В"]
TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}
ASSIGNMENT_HOUR_START = 8
ASSIGNMENT_HOUR_END = 12
SHEETS_RETRY_ATTEMPTS = 5
SHEETS_RETRY_BASE_SECONDS = 1.5
IMAP_TIMEOUT = 20
IMAP_LOOKBACK_DAYS = 3
IMAP_MAX_PER_DOCTOR = 20
IMAP_MAX_TOTAL = 100
LOG_HEADERS = [
    "Timestamp", "Event", "BOTON-ID", "Дата", "Врач", "Email",
    "Mapping JSON", "Message-ID", "Result", "Notes",
]
