"""Точка входа BoTon: ДЕЖУРНЫЙ РЕЖИМ.
Один запуск сам проверяет таблицу каждые 5 минут, отправляет рассылку,
как только появятся данные, и следит за новыми данными до конца окна.
"""
from time import sleep
from datetime import timedelta
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

# Утренний статус — если report.py обновлён. Если нет — работаем без него.
try:
    from report import send_morning_status
except ImportError:
    send_morning_status = None
    print("ℹ️ В report.py нет send_morning_status — утренние статусы отключены")

# Как часто проверять таблицу внутри дежурства
CHECK_INTERVAL_SECONDS = 300              # 5 минут
# Сколько живёт одно дежурство (лимит GitHub на job — 6 часов, берём с запасом)
MAX_WATCH_SECONDS = 5 * 3600 + 50 * 60    # 5 часов 50 минут


def make_client():
    """Свежий клиент Google — чтобы за часы дежурства не протух токен."""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def get_tasks_sheet_by_date(spreadsheet, target_date):
    """Ищет вкладку с именем-датой. Если нет — fallback. Если и его нет — (None, None)."""
    sheet_name = target_date.strftime(TASKS_SHEET_DATE_FORMAT)
    try:
        sheet = spreadsheet.worksheet(sheet_name)
        print(f"✅ Открыта вкладка: {sheet_name}")
        return sheet, sheet_name
    except gspread.WorksheetNotFound:
        print(f"⚠️ Вкладка '{sheet_name}' не найдена. Пробуем '{TASKS_SHEET_NAME_FALLBACK}'")
        try:
            sheet = spreadsheet.worksheet(TASKS_SHEET_NAME_FALLBACK)
            return sheet, TASKS_SHEET_NAME_FALLBACK
        except gspread.WorksheetNotFound:
            print(f"❌ Fallback-вкладка '{TASKS_SHEET_NAME_FALLBACK}' тоже не найдена!")
            return None, None


def notify(target_date, log_sheet, code, sheet_name="", stats=None):
    """Утренний статус заведующему (сам не дублируется — защита в report.py)."""
    if send_morning_status is None:
        return
    try:
        send_morning_status(target_date, log_sheet, code, sheet_name, stats)
    except Exception as exc:
        print(f"⚠️ Не удалось отправить статус '{code}': {exc}")


def run_once(cycle):
    """Одна проверка таблицы и, если есть данные, рассылка."""
    target_date = now_moscow().date()
    print(f"\n{'=' * 60}")
    print(f"ПРОВЕРКА №{cycle} | {now_moscow().strftime('%H:%M:%S')} МСК | "
          f"дата заданий {target_date.strftime('%d.%m.%Y')}")
    print("=" * 60)

    gc = make_client()

    log_spreadsheet = gc.open_by_key(LOG_SPREADSHEET_ID)
    log_sheet = ensure_log_sheet(log_spreadsheet)

    tasks_spreadsheet = gc.open_by_key(TASKS_SPREADSHEET_ID)
    tasks_sheet, used_sheet_name = get_tasks_sheet_by_date(tasks_spreadsheet, target_date)
    if tasks_sheet is None:
        notify(target_date, log_sheet, "NO_SHEET", "")
        return

    doctors_spreadsheet = gc.open_by_key(DOCTORS_SPREADSHEET_ID)
    doctors_sheet = doctors_spreadsheet.worksheet(DOCTORS_SHEET_NAME)
    doctors = load_doctors(doctors_sheet)
    print(f"👨‍⚕️ Загружено врачей: {len(doctors)}")
    if not doctors:
        notify(target_date, log_sheet, "NO_DOCTORS", used_sheet_name)
        return

    rows, stats, cols = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)

    if not rows:
        print("⚠️ Данных на сегодня в таблице пока нет. Проверю снова через 5 минут.")
        notify(target_date, log_sheet, "NO_DATA", used_sheet_name)
        return

    result = send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date, cols)
    print(f"\n🏁 Результат: {result}")

    if result.get("doctors", 0) > 0:
        send_summary_report(target_date, result, used_sheet_name)
        return

    # Никому не ушло — сообщаем причину (один раз в день)
    if result.get("errors", 0) > 0:
        code = "ERROR"
    elif result.get("not_found", 0) > 0:
        code = "NOT_FOUND"
    elif result.get("skipped", 0) > 0:
        code = "ALREADY_SENT"
    else:
        code = "NO_DATA"
    notify(target_date, log_sheet, code, used_sheet_name, result)


def main():
    started = now_moscow()
    deadline = started + timedelta(seconds=MAX_WATCH_SECONDS)
    print(f"🟢 ДЕЖУРСТВО НАЧАТО {started.strftime('%d.%m.%Y %H:%M')} МСК")
    print(f"   Окно рассылки: {ASSIGNMENT_HOUR_START:02d}:00–{ASSIGNMENT_HOUR_END:02d}:00 МСК")
    print(f"   Проверка каждые {CHECK_INTERVAL_SECONDS // 60} минут")
    print(f"   Дежурство до: {deadline.strftime('%H:%M')} МСК")

    cycle = 0
    while True:
        now = now_moscow()

        if now >= deadline:
            print("\n⏹️ Дежурство окончено: вышло время запуска. "
                  "Следующее дежурство по расписанию.")
            return

        if now.hour >= ASSIGNMENT_HOUR_END:
            print(f"\n⏹️ Окно рассылки закрыто ({now.strftime('%H:%M')} МСК). "
                  f"Дежурство окончено.")
            return

        if now.hour < ASSIGNMENT_HOUR_START:
            # Запуск случился ночью/рано утром — НЕ выходим, ждём открытия окна.
            print(f"⏳ Сейчас {now.strftime('%H:%M')} МСК — рано. "
                  f"Жду {ASSIGNMENT_HOUR_START:02d}:00, проверяю каждые 5 минут.")
            sleep(min(CHECK_INTERVAL_SECONDS,
                      max(1, int((deadline - now).total_seconds()))))
            continue

        cycle += 1
        try:
            run_once(cycle)
        except Exception as exc:
            print(f"❌ Ошибка проверки №{cycle}: {exc}")
            print("   Не страшно — повторю через 5 минут.")

        left = int((deadline - now_moscow()).total_seconds())
        if left <= 0:
            continue
        print(f"\n💤 Пауза {min(CHECK_INTERVAL_SECONDS, left) // 60} мин "
              f"(дежурство ещё {left // 60} мин)")
        sleep(min(CHECK_INTERVAL_SECONDS, left))


if __name__ == "__main__":
    main()
