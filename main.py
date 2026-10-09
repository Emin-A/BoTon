"""Точка входа BoTon."""
from config import *
from utils import now_moscow, is_assignment_time
from google_sheets import get_google_client, open_tasks_sheet, open_doctors_sheet
from log_store import ensure_log_sheet
from doctors import load_doctors_map
from tasks import load_task_rows, print_stats
from mailer import send_doctor_diagnostic
from receiver import process_incoming_messages
from sender import send_today_assignments
from report import send_summary_report

def main():
    print("=" * 60)
    print(f"BoTon v{VERSION}")
    print(f"FORCE_RESEND = {FORCE_RESEND}")
    print(f"TEST_TO_RESERVE = {TEST_TO_RESERVE}")
    print(f"TEST_RECEIVE_ONLY = {TEST_RECEIVE_ONLY}")
    print(f"TEST_DOCTOR_DIAG = {TEST_DOCTOR_DIAG}")
    if TEST_DOCTOR_DIAG:
        print(f"TEST_EMAIL = {TEST_EMAIL}")
    print("=" * 60)

    target_date = now_moscow().date()
    print(f"Дата: {target_date.strftime('%d.%m.%Y')}")
    print(f"Время МСК: {now_moscow().strftime('%d.%m.%Y %H:%M:%S')}")

    client = get_google_client()
    print("Подключение к Google Sheets: OK")

    tasks_sheet, book = open_tasks_sheet(client)
    log_sheet = ensure_log_sheet(book)
    doctors_sheet = open_doctors_sheet(client)
    doctors = load_doctors_map(doctors_sheet)

    # ------------------- СБОР СТРОК -------------------
    rows, stats = load_task_rows(tasks_sheet, target_date)
    print_stats(stats)

    # ------------------- ДИАГНОСТИКА -------------------
    if TEST_DOCTOR_DIAG:
        send_doctor_diagnostic(rows, doctors, target_date)
        print("\nРежим TEST_DOCTOR_DIAG=True — основная рассылка и приём ответов пропущены.")
        print(f"BoTon v{VERSION} завершён (диагностика).")
        return

    # ------------------- ПРИЁМ ОТВЕТОВ -------------------
    received = process_incoming_messages(tasks_sheet, log_sheet, doctors)

    # ------------------- РАССЫЛКА -------------------
    send_stats = {
        "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
        "skipped": 0, "reserve": 0, "not_found": 0,
    }

    if TEST_RECEIVE_ONLY:
        print("\n----- РАССЫЛКА ОТКЛЮЧЕНА (TEST_RECEIVE_ONLY=True) -----")
    elif not rows:
        print("\n----- РАССЫЛКА ПРОПУЩЕНА (0 подходящих строк на сегодня) -----")
    else:
        allowed = is_assignment_time() or FORCE_RESEND or TEST_TO_RESERVE
        if allowed:
            if TEST_TO_RESERVE:
                print("\n----- ТЕСТОВЫЙ ПРОГОН: СВОДНОЕ ПИСЬМО → НА РЕЗЕРВ -----")
            elif FORCE_RESEND:
                print("\n----- ПРИНУДИТЕЛЬНАЯ РАССЫЛКА (FORCE_RESEND) -----")
            else:
                print("\n----- ПЛАНОВАЯ РАССЫЛКА (окно 08:00–12:00 МСК) -----")
            send_stats = send_today_assignments(
                tasks_sheet, log_sheet, doctors, rows, target_date
            )
        else:
            print("\n----- РАССЫЛКА НЕ ВЫПОЛНЯЕТСЯ -----")
            print("Вне окна 08:00–12:00 МСК. FORCE_RESEND=False.")


    # ------------------- ОТЧЁТ -------------------
    # Отчёт отправляем только если было что отчитываться:
    #   - были отправки врачам (> 0),
    #   - или обработаны ответы врачей (> 0),
    #   - или это принудительный / диагностический прогон.
    has_send    = send_stats.get("doctors", 0) > 0
    has_receive = received > 0
    force_report = FORCE_RESEND or TEST_TO_RESERVE or TEST_DOCTOR_DIAG

    if has_send or has_receive or force_report:
        send_summary_report(target_date, send_stats, received, tasks_sheet.title)
    else:
        print("\nОтчёт не отправляется: 0 отправок и 0 ответов (пустой прогон).")