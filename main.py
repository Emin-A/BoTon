"""Точка входа: поиск вкладки с сегодняшней датой и запуск рассылки."""
import sys
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from config import (
    TASKS_SPREADSHEET_ID,
    TASKS_SHEET_DATE_FORMAT,
    TASKS_SHEET_NAME_FALLBACK,
    DOCTORS_SPREADSHEET_ID,
    DOCTORS_SHEET_NAME,
    LOG_SHEET_NAME,
    SCOPES,
    SERVICE_ACCOUNT_FILE,
)
from tasks import load_task_rows, print_stats
from sender import send_today_assignments
from doctors import load_doctors


def get_tasks_sheet_by_date(spreadsheet, target_date):
    """Ищет вкладку с именем в формате даты. Если не найдена — использует fallback."""
    sheet_name = target_date.strftime(TASKS_SHEET_DATE_FORMAT)
    try:
        sheet = spreadsheet.worksheet(sheet_name)
        print(f"✅ Открыта вкладка: {sheet_name}")
        return sheet, sheet_name
    except gspread.WorksheetNotFound:
        print(f"⚠️ Вкладка '{sheet_name}' не найдена. Используем fallback: '{TASKS_SHEET_NAME_FALLBACK}'")
        try:
            sheet = spreadsheet.worksheet(TASKS_SHEET_NAME_FALLBACK)
            return sheet, TASKS_SHEET_NAME_FALLBACK
        except gspread.WorksheetNotFound:
            print(f"❌ Fallback-вкладка '{TASKS_SHEET_NAME_FALLBACK}' тоже не найдена!")
            sys.exit(1)


def main():
    # Авторизация в Google Sheets
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    # Сегодняшняя дата
    target_date = datetime.now().date()
    print(f"📅 Дата рассылки: {target_date.strftime('%d.%m.%Y')}")

    # Таблица заданий
    tasks_spreadsheet = gc.open_by_key(TASKS_SPREADSHEET_ID)
    tasks_sheet, used_sheet_name = get_tasks_sheet_by_date(tasks_spreadsheet, target_date)

    # Таблица врачей
    doctors_spreadsheet = gc.open_by_key(DOCTORS_SPREADSHEET_ID)
    doctors_sheet = doctors_spreadsheet.worksheet(DOCTORS_SHEET_NAME)
    doctors = load_doctors(doctors_sheet)
    print(f"👨‍️ Загружено врачей: {len(doctors)}")

    # Журнал
    log_sheet = tasks_spreadsheet.worksheet(LOG_SHEET_NAME)

    # Загрузка строк
    rows, stats = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)

    if not rows:
        print("⚠️ Нет строк для отправки. Завершаю работу.")
        return

    # Отправка
    result = send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date)
    print(f"\n🏁 Рассылка завершена. Результат: {result}")


if __name__ == "__main__":
    main()
