"""Отчёт заведующему после рассылки."""
from config import *
from utils import now_moscow
from mailer import send_email


def send_summary_report(target_date, send_stats, sheet_name):
    print("\n----- ФОРМИРОВАНИЕ И ОТПРАВКА ОТЧЁТА -----")
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    test_mark = " (ТЕСТОВЫЙ ПРОГОН)" if TEST_TO_RESERVE else ""
    text_body = (
        f"ОТЧЁТ СИСТЕМЫ BoTon v{VERSION}{resend_mark}{test_mark}\n"
        f"Дата формирования: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК\n"
        f"{'=' * 50}\n\n"
        f"ИСТОЧНИК ДАННЫХ:\n"
        f"  Вкладка: {sheet_name or 'НЕ НАЙДЕНА'}\n\n"
        f"СТАТИСТИКА РАССЫЛКИ:\n"
        f"  Врачей обработано: .............. {send_stats.get('doctors', 0)}\n"
        f"  Успешно отправлено: ............. "
        f"{send_stats.get('doctors', 0) - send_stats.get('errors', 0)}\n"
        f"  Отправлено на резерв: ........... {send_stats.get('reserve', 0)}\n"
        f"  Ошибок отправки: ................ {send_stats.get('errors', 0)}\n"
        f"  Пропущено по журналу: ........... {send_stats.get('skipped', 0)}\n"
        f"  Врач не найден в базе: .......... {send_stats.get('not_found', 0)}\n"
        f"  Строк обновлено (комментарии): .. {send_stats.get('rows', 0)}\n"
        f"  Всего заданий в рассылке: ....... {send_stats.get('tasks', 0)}\n\n"
        f"{'=' * 50}\n"
        f"Система работает автоматически.\n"
        f"По вопросам — к администратору."
    )
    subject = (
        f"[ОТЧЁТ BoTon] Рассылка за {target_date.strftime('%d.%m.%Y')}"
        f"{resend_mark}{test_mark}"
    )
    for addr in RESERVE_EMAILS:
        try:
            send_email(addr, subject, text_body)
            print(f"✅ ОТЧЁТ ОТПРАВЛЕН на {addr}")
        except Exception as exc:
            print(f"❌ ОШИБКА ОТПРАВКИ ОТЧЁТА на {addr}: {exc}")
