"""Подключение к Google Sheets и пакетные операции."""
import os
import json
from time import sleep
import gspread
from google.oauth2.service_account import Credentials
from config import *


def sheets_call(operation, description="Google Sheets"):
    """Выполняет операцию с повторными попытками при временных ошибках."""
    last_error = None
    for attempt in range(1, SHEETS_RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            retryable = any(k in text for k in (
                "429", "quota exceeded", "rate limit",
                "500", "502", "503", "504",
                "backend error", "service unavailable",
            ))
            if not retryable or attempt >= SHEETS_RETRY_ATTEMPTS:
                raise
            delay = SHEETS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            print(f"Google Sheets: временная ошибка ({exc}). "
                  f"Повтор {attempt}/{SHEETS_RETRY_ATTEMPTS} через {delay:.1f} сек.")
            sleep(delay)
    raise last_error


def get_google_client():
    """Создаёт клиент Google из секрета GOOGLE_SERVICE_ACCOUNT."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    if not raw:
        raise RuntimeError("Не найден GOOGLE_SERVICE_ACCOUNT")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_tasks_sheet(client):
    """Открывает fallback-лист таблицы заданий."""
    book = client.open_by_key(TASKS_SPREADSHEET_ID)
    ws = book.worksheet(TASKS_SHEET_NAME_FALLBACK)
    print(f"Использую вкладку: '{TASKS_SHEET_NAME_FALLBACK}'")
    return ws, book


def open_doctors_sheet(client):
    """Открывает лист базы врачей."""
    return client.open_by_key(DOCTORS_SPREADSHEET_ID).worksheet(DOCTORS_SHEET_NAME)


def batch_update_cells(sheet, updates, description="пакетная запись"):
    """Пакетная запись ячеек. updates = [{'row':.., 'col':.., 'value':..}, ...]"""
    if not updates:
        return
    data = []
    for item in updates:
        a1 = gspread.utils.rowcol_to_a1(int(item["row"]), int(item["col"]))
        data.append({"range": f"{a1}:{a1}", "values": [[item.get("value", "")]]})
    sheets_call(
        lambda: sheet.batch_update(data, value_input_option="USER_ENTERED"),
        description,
    )
