
"""Общие настройки движка BoTon."""

from zoneinfo import ZoneInfo

VERSION = "3.4"

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Режимы работы
FORCE_RESEND = False
TEST_TO_RESERVE = False
TEST_RECEIVE_ONLY = False
TEST_DOCTOR_DIAG = False

# Диагностика и отчёты
TEST_EMAIL = "agaverdiev@mail.ru"
REPORT_EMAIL = "eagaverdiev@mosmedzdrav.ru"

# Временное окно отправки
ASSIGNMENT_HOUR_START = 8
ASSIGNMENT_HOUR_END = 12

# Повторные запросы к Google Sheets
SHEETS_RETRY_ATTEMPTS = 5
SHEETS_RETRY_BASE_SECONDS = 1.5

# Получение почты
IMAP_TIMEOUT = 20
IMAP_LOOKBACK_DAYS = 3
IMAP_MAX_PER_DOCTOR = 20
IMAP_MAX_TOTAL = 100
