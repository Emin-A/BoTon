"""Точка входа: поиск вкладки с сегодняшней датой и запуск рассылки."""
import sys
import gspread
from google.oauth2.service_account import Credentials
from config import (
    TASKS_SPREADSHEET_ID,
    TASKS_SHEET_DATE_FORMAT,
    TASKS_SHEET_NAME_FALLBACK,
    DOCTORS_SPREADSHEET_ID,
    DOCTORS_SHEET_NAME,
    LOG_SPREADSHEET_ID,
    SCOPES,
    SERVICE_ACCOUNT_FILE,
    ASSIGNMENT_HOUR_START,
    ASSIGNMENT_HOUR_END,
)
from tasks import load_task_rows, print_stats
from sender import send_today_assignments
from doctors import load_doctors
from log_store import ensure_log_sheet
from report import send_summary_report
from utils import now_moscow


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


def is_within_work_window():
    """Проверяет, что текущее московское время внутри окна рассылки."""
    now = now_moscow()
    current_minutes = now.hour * 60 + now.minute
    return ASSIGNMENT_HOUR_START * 60 <= current_minutes < ASSIGNMENT_HOUR_END * 60


def main():
    # 1. Вне окна 07:00–14:00 МСК — выходим без отправки.
    if not is_within_work_window():
        now_str = now_moscow().strftime("%H:%M")
        print(f"⏸️ Сейчас {now_str} МСК — вне окна рассылки "
              f"({ASSIGNMENT_HOUR_START:02d}:00 – {ASSIGNMENT_HOUR_END:02d}:00). "
              f"Завершаю без отправки.")
        return

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    target_date = now_moscow().date()
    print(f"📅 Дата рассылки: {target_date.strftime('%d.%m.%Y')}")
    print(f"🕐 Время запуска (МСК): {now_moscow().strftime('%H:%M:%S')}")

    # 2. Таблица заданий
    tasks_spreadsheet = gc.open_by_key(TASKS_SPREADSHEET_ID)
    tasks_sheet, used_sheet_name = get_tasks_sheet_by_date(tasks_spreadsheet, target_date)

    # 3. Таблица врачей
    doctors_spreadsheet = gc.open_by_key(DOCTORS_SPREADSHEET_ID)
    doctors_sheet = doctors_spreadsheet.worksheet(DOCTORS_SHEET_NAME)
    doctors = load_doctors(doctors_sheet)
    print(f"👨‍⚕️ Загружено врачей: {len(doctors)}")

    # 4. Журнал — создаётся автоматически, если отсутствует
    log_spreadsheet = gc.open_by_key(LOG_SPREADSHEET_ID)
    log_sheet = ensure_log_sheet(log_spreadsheet)

    # 5. Загрузка строк + автоопределение колонок
    rows, stats, cols = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)

    # 6. Данных нет — тихо выходим. Cron запустит проверку снова через 5 минут.
    if not rows:
        print("⚠️ Данных на сегодня в таблице пока нет. "
              "Завершаю — следующий запуск через 5 минут проверит снова.")
        return

    # 7. Отправка
    result = send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date, cols)
    print(f"\n🏁 Рассылка завершена. Результат: {result}")

    # 8. Отчёт заведующему — только если была реальная отправка
    if result.get("doctors", 0) > 0:
        send_summary_report(target_date, result, used_sheet_name)


if __name__ == "__main__":
    main()
