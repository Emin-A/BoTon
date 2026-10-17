"""Централизованные настройки BoTon. Секреты хранятся только в GitHub Secrets."""
import os
from datetime import time
from zoneinfo import ZoneInfo

VERSION = "4.1"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _env_flag(name):
    """True, только если переменная окружения равна 'true'."""
    return os.environ.get(name, "").strip().lower() == "true"


# Режимы запуска включаются кнопкой в GitHub (Run workflow).
FORCE_RESEND = _env_flag("FORCE_RESEND")
TEST_TO_RESERVE = _env_flag("TEST_TO_RESERVE")

TEST_EMAIL = "agaverdiev@mail.ru"
REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"

# Резервные адреса: сюда уходят письма в тестовом режиме
# и когда у врача не заполнен email.
RESERVE_EMAILS = [
    "eagaverdiev@mosmedzdrav.ru",
    "agaverdiev@mail.ru",
]

# Если запись PENDING висит дольше этого времени — запуск считает отправку
# погибшей и отправляет письмо повторно.
PENDING_RETRY_MINUTES = 5

SERVICE_ACCOUNT_FILE = "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Таблица заданий
TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_DATE_FORMAT = "%d.%m.%Y"
TASKS_SHEET_NAME_FALLBACK = "Лист1"

# База врачей
DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

# Журнал бота (вкладка BoTon_Log в таблице врачей)
LOG_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
LOG_SHEET_NAME = "BoTon_Log"

# Резервные номера столбцов (индексация с нуля).
# Реальные колонки определяются АВТОМАТИЧЕСКИ по заголовкам (см. tasks.py).
COL_DATE = 0
COL_BRANCH = 1
COL_DOCTOR = 2
COL_PATIENT_ID = 3
COL_PATIENT_NAME = 4
COL_SOURCE = 5
COL_TASK1 = 6
COL_TASK2 = 7
COL_TASK3 = 8
COL_COMMENTS = 9  # колонка «Комментарии»: сюда sender пишет отметку об отправке

BRANCH_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]

TASK_LETTERS = ["А", "Б", "В"]

TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}

# Окно рассылки по московскому времени
ASSIGNMENT_HOUR_START = 7
ASSIGNMENT_HOUR_END = 23

SHEETS_RETRY_ATTEMPTS = 5
SHEETS_RETRY_BASE_SECONDS = 1.5

# Почта (SMTP): значения берутся из секретов GitHub Actions
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", REPORT_EMAIL)

LOG_HEADERS = [
    "Timestamp",
    "Event",
    "BOTON-ID",
    "Дата",
    "Врач",
    "Email",
    "Mapping JSON",
    "Message-ID",
    "Result",
    "Notes",
]
