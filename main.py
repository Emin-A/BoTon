"""Точка входа: проверка данных, рассылка и утренний статус заведующему."""
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
from report import send_summary_report, send_morning_status
from utils import now_moscow


def get_tasks_sheet_by_date(spreadsheet, target_date):
    """Ищет вкладку с именем-датой. Если нет — fallback. Если и его нет — (None, None)."""
    sheet_name = target_date.strftime(TASKS_SHEET_DATE_FORMAT)
    try:
        sheet = spreadsheet.worksheet(sheet_name)
        print(f"✅ Открыта вкладка: {sheet_name}")
        return sheet, sheet_name
    except gspread.WorksheetNotFound:
        print(f"⚠️ Вкладка '{sheet_name}' не найдена. Пробуем fallback: '{TASKS_SHEET_NAME_FALLBACK}'")
        try:
            sheet = spreadsheet.worksheet(TASKS_SHEET_NAME_FALLBACK)
            return sheet, TASKS_SHEET_NAME_FALLBACK
        except gspread.WorksheetNotFound:
            print(f"❌ Fallback-вкладка '{TASKS_SHEET_NAME_FALLBACK}' тоже не найдена!")
            return None, None


def main():
    now = now_moscow()

    # 1. Вне окна рассылки — тихо выходим, писем и статусов не шлём.
    if not (ASSIGNMENT_HOUR_START <= now.hour < ASSIGNMENT_HOUR_END):
        print(f"⏸️ Сейчас {now.strftime('%H:%M')} МСК — вне окна рассылки "
              f"({ASSIGNMENT_HOUR_START:02d}:00–{ASSIGNMENT_HOUR_END:02d}:00). Выход.")
        return

    target_date = now.date()
    print(f"📅 Дата рассылки: {target_date.strftime('%d.%m.%Y')}")
    print(f"🕐 Время запуска (МСК): {now.strftime('%H:%M:%S')}")

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    # 2. Журнал — нужен и для рассылки, и для статусов
    log_spreadsheet = gc.open_by_key(LOG_SPREADSHEET_ID)
    log_sheet = ensure_log_sheet(log_spreadsheet)

    # 3. Таблица заданий
    tasks_spreadsheet = gc.open_by_key(TASKS_SPREADSHEET_ID)
    tasks_sheet, used_sheet_name = get_tasks_sheet_by_date(tasks_spreadsheet, target_date)
    if tasks_sheet is None:
        send_morning_status(target_date, log_sheet, "NO_SHEET", "")
        sys.exit(1)

    # 4. База врачей
    doctors_spreadsheet = gc.open_by_key(DOCTORS_SPREADSHEET_ID)
    doctors_sheet = doctors_spreadsheet.worksheet(DOCTORS_SHEET_NAME)
    doctors = load_doctors(doctors_sheet)
    print(f"👨‍⚕️ Загружено врачей: {len(doctors)}")
    if not doctors:
        send_morning_status(target_date, log_sheet, "NO_DOCTORS", used_sheet_name)
        return

    # 5. ПРОВЕРКА: есть ли в таблице данные для рассылки
    rows, stats, cols = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)

    if not rows:
        print("⚠️ Данных на сегодня в таблице нет. Рассылка не отправлена.")
        send_morning_status(target_date, log_sheet, "NO_DATA", used_sheet_name)
        return

    # 6. Рассылка
    result = send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date, cols)
    print(f"\n🏁 Рассылка завершена. Результат: {result}")

    # 7. Отчёт заведующему
    if result.get("doctors", 0) > 0:
        send_summary_report(target_date, result, used_sheet_name)
        return

    # 8. Никому не ушло — сообщаем причину
    if result.get("errors", 0) > 0:
        code = "ERROR"
    elif result.get("not_found", 0) > 0:
        code = "NOT_FOUND"
    elif result.get("skipped", 0) > 0:
        code = "ALREADY_SENT"
    else:
        code = "NO_DATA"
    send_morning_status(target_date, log_sheet, code, used_sheet_name, result)


if __name__ == "__main__":
    main()
