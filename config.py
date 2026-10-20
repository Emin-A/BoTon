"""Централизованные настройки BoTon.
Все персональные данные и адреса таблиц берутся ТОЛЬКО из секретов GitHub."""
import os
from datetime import time
from zoneinfo import ZoneInfo

VERSION = "4.3"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _env_flag(name):
    """True, только если переменная окружения равна 'true'."""
    return os.environ.get(name, "").strip().lower() == "true"


def _env_list(name):
    """Список значений из переменной окружения через запятую."""
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


# Режимы запуска включаются кнопкой в GitHub (Run workflow).
FORCE_RESEND = _env_flag("FORCE_RESEND")
TEST_TO_RESERVE = _env_flag("TEST_TO_RESERVE")

# Персональные адреса — ТОЛЬКО из секретов GitHub
TEST_EMAIL = os.environ.get("TEST_EMAIL", "")
REPORT_EMAIL = os.environ.get("REPORT_EMAIL", "")

RESERVE_EMAILS = _env_list("RESERVE_EMAILS")
if not RESERVE_EMAILS:
    RESERVE_EMAILS = [a for a in [REPORT_EMAIL, TEST_EMAIL] if a]

# Адреса таблиц — ТОЛЬКО из секретов GitHub
TASKS_SPREADSHEET_ID = os.environ.get("TASKS_SPREADSHEET_ID", "")
DOCTORS_SPREADSHEET_ID = os.environ.get("DOCTORS_SPREADSHEET_ID", "")
LOG_SPREADSHEET_ID = os.environ.get("LOG_SPREADSHEET_ID", "")

SERVICE_ACCOUNT_FILE = "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TASKS_SHEET_DATE_FORMAT = "%d.%m.%Y"
TASKS_SHEET_NAME_FALLBACK = "Лист1"
DOCTORS_SHEET_NAME = "Врачи"
LOG_SHEET_NAME = "BoTon_Log"

# Если запись PENDING висит дольше этого времени — отправка повторяется.
PENDING_RETRY_MINUTES = 5

# Резервные номера столбцов (индексация с нуля).
# Реальные колонки и КОЛИЧЕСТВО заданий определяются по заголовкам (см. tasks.py).
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

# Буквы для нескольких заданий у одного пациента (до 10 заданий)
TASK_LETTERS = ["А", "Б", "В", "Г", "Д", "Е", "Ж", "З", "И", "К"]

# Заголовки заданий по умолчанию (если шапка не распознана)
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

# Проверка: все обязательные секреты заданы
REQUIRED_SETTINGS = {
    "TEST_EMAIL": TEST_EMAIL,
    "REPORT_EMAIL": REPORT_EMAIL,
    "TASKS_SPREADSHEET_ID": TASKS_SPREADSHEET_ID,
    "DOCTORS_SPREADSHEET_ID": DOCTORS_SPREADSHEET_ID,
    "LOG_SPREADSHEET_ID": LOG_SPREADSHEET_ID,
}

_missing = [k for k, v in REQUIRED_SETTINGS.items() if not v]
if _missing:
    raise RuntimeError(
        "Не заданы секреты/переменные окружения: " + ", ".join(_missing) +
        ". Добавьте их: GitHub → Settings → Secrets and variables → Actions."
    )
