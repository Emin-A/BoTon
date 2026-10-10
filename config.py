"""Централизованные настройки BoTon. Секреты хранятся только в GitHub Secrets."""
import os
from datetime import time
from zoneinfo import ZoneInfo

VERSION = "3.3"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _env_flag(name):
    """True, только если переменная окружения равна 'true'."""
    return os.environ.get(name, "").strip().lower() == "true"


# Режимы запуска включаются кнопкой в GitHub (Run workflow),
# а не правкой кода. При обычном запуске по расписанию все равны False.
FORCE_RESEND = _env_flag("FORCE_RESEND")
TEST_TO_RESERVE = _env_flag("TEST_TO_RESERVE")
TEST_RECEIVE_ONLY = _env_flag("TEST_RECEIVE_ONLY")
TEST_DOCTOR_DIAG = False

TEST_EMAIL = "agaverdiev@mail.ru"
REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"

# Авторизация Google Sheets (файл создаётся в GitHub Actions из секрета)
SERVICE_ACCOUNT_FILE = "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Таблица заданий
TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
TASKS_SHEET_NAME = "Лист1"  # оставлено для совместимости со старыми модулями
# Формат имени вкладки с датой (например, "10.10.2026")
TASKS_SHEET_DATE_FORMAT = "%d.%m.%Y"
# Fallback: если вкладка с сегодняшней датой не найдена
TASKS_SHEET_NAME_FALLBACK = "Лист1"

# База врачей
DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"
DOCTORS_SHEET_NAME = "Врачи"

# Журнал бота
LOG_SHEET_NAME = "BoTon_Log"

# Номера столбцов: индексация с нуля
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
STATUS_PRIORITY = {
    "": 0,
    "❌ Не выполнено": 1,
    "⚠️ Частично": 2,
    "✅ Выполнено": 3,
}

# Допустимые обозначения филиала (БЕЗ пробелов в конце!)
BRANCH_KEYWORDS = [
    "ф2",
    "филиал 2",
    "филиал № 2",
    "гп 107 ф 2",
    "гп 48",
    "107 ф 2",
    "гп 107 ф2",
]

# Буквы заданий (БЕЗ пробелов!)
TASK_LETTERS = ["А", "Б", "В"]

# Названия заданий (БЕЗ пробелов в конце!)
TASK_TITLES = {
    1: "Задание 1 (липиды)",
    2: "Задание 2 (КДП — выгрузка)",
    3: "Задание 3 (КДП СД 3 квартал)",
}

# Окно рассылки по московскому времени
ASSIGNMENT_HOUR_START = 8
ASSIGNMENT_HOUR_END = 12

# Повторные попытки обращения к Google Sheets
SHEETS_RETRY_ATTEMPTS = 5
SHEETS_RETRY_BASE_SECONDS = 1.5

# Ограничения IMAP
IMAP_TIMEOUT = 20
IMAP_LOOKBACK_DAYS = 3
IMAP_MAX_PER_DOCTOR = 20
IMAP_MAX_TOTAL = 100

# Почта (SMTP): значения берутся из секретов GitHub Actions
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", REPORT_EMAIL)

# Почта (IMAP): значения берутся из секретов GitHub Actions
IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")

# Заголовки журнала (БЕЗ пробелов в конце!)
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
