"""Отчёт заведующему после рассылки + утренний статус с причиной."""
from config import *
from utils import now_moscow
from mailer import send_email
from log_store import load_log_records, get_send_state_age, append_log_entries


# code: (заголовок, что случилось, что делать)
STATUS_INFO = {
    "NO_DATA": (
        "В ТАБЛИЦЕ ЗАДАНИЙ НЕТ ДАННЫХ НА СЕГОДНЯ",
        "Строк на сегодняшнюю дату не найдено: вкладка пустая, "
        "или в строках нет ни одного задания.",
        "Заполните вкладку с сегодняшней датой в таблице заданий. "
        "Бот подхватит данные сам при следующей проверке.",
    ),
    "NO_SHEET": (
        "НЕ НАЙДЕНА ВКЛАДКА С СЕГОДНЯШНЕЙ ДАТОЙ",
        "В таблице заданий нет вкладки с именем ДД.ММ.ГГГГ "
        "и нет резервной вкладки.",
        "Создайте вкладку с сегодняшней датой (например, 21.11.2026) "
        "и заполните её.",
    ),
    "NO_DOCTORS": (
        "БАЗА ВРАЧЕЙ ПУСТА",
        "В листе «Врачи» нет ни одной строки с ФИО.",
        "Заполните лист «Врачи»: ФИО, Email, Филиал.",
    ),
    "NOT_FOUND": (
        "ВРАЧИ ИЗ ЗАДАНИЙ НЕ НАЙДЕНЫ В БАЗЕ ВРАЧЕЙ",
        "ФИО из таблицы заданий не совпали ни с одним ФИО в листе «Врачи».",
        "Сверьте написание ФИО в обеих таблицах — они должны совпадать.",
    ),
    "ALREADY_SENT": (
        "РАССЫЛКА НА СЕГОДНЯ УЖЕ БЫЛА ОТПРАВЛЕНА РАНЕЕ",
        "Все задания помечены в журнале как отправленные. "
        "Повторно врачам НЕ отправляем — это защита от дублей.",
        "Ничего делать не нужно. Врачи уже получили письма.",
    ),
    "ERROR": (
        "ОШИБКА ОТПРАВКИ ПИСЕМ (SMTP)",
        "Письма не ушли из-за ошибки почтового сервера. "
        "Текст ошибки записан в журнал BoTon_Log и в лог запуска.",
        "Проверьте секреты SMTP_HOST / SMTP_USER / SMTP_PASSWORD "
        "и лог запуска в GitHub Actions.",
    ),
}


def _format_per_doctor(per_doctor):
    """Форматирует список врачей в строки вида 'Фамилия И.О. — X заданий'."""
    if not per_doctor:
        return ["  (нет данных)"]
    sorted_docs = sorted(
        per_doctor,
        key=lambda d: d["doctor"].split()[0] if d["doctor"] else ""
    )
    lines = []
    for d in sorted_docs:
        name = d["doctor"]
        tasks = d["tasks"]
        patients = d.get("patients", 0)
        lines.append(f"  {name} — {tasks} заданий ({patients} пациентов)")
    return lines


def _status_recipients():
    """Заведующий + резервные адреса, без повторов."""
    addrs = []
    for a in [REPORT_EMAIL] + list(RESERVE_EMAILS):
        a = (a or "").strip()
        if a and a not in addrs:
            addrs.append(a)
    return addrs


def send_morning_status(target_date, log_sheet, code, sheet_name="", stats=None):
    """Утренний статус заведующему: ушла рассылка или нет и почему.
    Отправляется ОДИН раз в день на каждую причину (защита от спама)."""
    title, what, todo = STATUS_INFO.get(
        code, ("СТАТУС РАССЫЛКИ", "См. журнал BoTon_Log.", "Проверьте лог запуска.")
    )

    date_str = target_date.strftime("%d.%m.%Y")
    bot_id = f"STATUS-{date_str}-{code}"

    # --- защита от повторной отправки статуса ---
    state, age_min = None, None
    try:
        records = load_log_records(log_sheet)
        state, age_min = get_send_state_age(records, bot_id)
    except Exception as exc:
        print(f"⚠️ Не удалось прочитать журнал статусов: {exc}")

    if not FORCE_RESEND:
        if state == "SENT":
            print(f"Статус '{code}' уже отправлялся сегодня — пропускаю")
            return False
        if state == "PENDING" and (age_min is None or age_min < PENDING_RETRY_MINUTES):
            print(f"Статус '{code}' в процессе отправки — пропускаю")
            return False

    stats = stats or {}
    test_mark = " (ТЕСТОВЫЙ ПРОГОН)" if TEST_TO_RESERVE else ""

    body = (
        f"УТРЕННИЙ СТАТУС РАССЫЛКИ BoTon v{VERSION}{test_mark}\n"
        f"Дата заданий: {date_str}\n"
        f"Время проверки: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК\n"
        f"{'=' * 50}\n\n"
        f"РЕЗУЛЬТАТ: РАССЫЛКА ВРАЧАМ НЕ ОТПРАВЛЕНА\n\n"
        f"ПРИЧИНА: {title}\n\n"
        f"ЧТО ПРОИЗОШЛО:\n  {what}\n\n"
        f"ЧТО ДЕЛАТЬ:\n  {todo}\n\n"
        f"ИСТОЧНИК ДАННЫХ:\n"
        f"  Вкладка: {sheet_name or 'НЕ НАЙДЕНА'}\n\n"
        f"ЦИФРЫ ЭТОЙ ПРОВЕРКИ:\n"
        f"  Пропущено (уже отправлено): ..... {stats.get('skipped', 0)}\n"
        f"  Врач не найден в базе: .......... {stats.get('not_found', 0)}\n"
        f"  Ошибок отправки: ................ {stats.get('errors', 0)}\n"
        f"  Всего заданий в таблице: ........ {stats.get('tasks', 0)}\n\n"
        f"{'=' * 50}\n"
        f"Бот проверяет таблицу каждые 5 минут\n"
        f"с {ASSIGNMENT_HOUR_START:02d}:00 до {ASSIGNMENT_HOUR_END:02d}:00 МСК.\n"
        f"Как только данные появятся — рассылка уйдёт автоматически,\n"
        f"и вам придёт обычный отчёт об отправке.\n"
        f"Повторно это уведомление сегодня не придёт."
    )

    subject = f"[BoTon] Рассылка за {date_str} — НЕ ОТПРАВЛЕНА: {title}{test_mark}"

    recipients = _status_recipients()
    if not recipients:
        print("❌ Не заданы адреса для статуса (REPORT_EMAIL / RESERVE_EMAILS)")
        return False

    log_entry_base = {
        "event": "SEND",
        "bot_id": bot_id,
        "target_date": date_str,
        "doctor": "Заведующий (статус рассылки)",
        "email_address": ",".join(recipients),
        "mapping": {},
        "notes": f"УТРЕННИЙ СТАТУС: {code}",
    }

    try:
        append_log_entries(log_sheet, [dict(log_entry_base, result="PENDING")])
    except Exception as exc:
        print(f"⚠️ Не удалось записать PENDING статуса: {exc}")

    ok = False
    for addr in recipients:
        try:
            send_email(addr, subject, body)
            print(f"✅ СТАТУС '{code}' ОТПРАВЛЕН на {addr}")
            ok = True
        except Exception as exc:
            print(f"❌ ОШИБКА ОТПРАВКИ СТАТУСА на {addr}: {exc}")

    try:
        append_log_entries(log_sheet, [
            dict(log_entry_base, result="SENT" if ok else "ERROR")
        ])
    except Exception as exc:
        print(f"⚠️ Не удалось записать результат статуса: {exc}")

    return ok


def send_summary_report(target_date, send_stats, sheet_name):
    print("\n----- ФОРМИРОВАНИЕ И ОТПРАВКА ОТЧЁТА -----")
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    test_mark = " (ТЕСТОВЫЙ ПРОГОН)" if TEST_TO_RESERVE else ""

    per_doctor = send_stats.get("per_doctor") or []
    per_doctor_lines = _format_per_doctor(per_doctor)

    text_body = (
        f"ОТЧЁТ СИСТЕМЫ BoTon v{VERSION}{resend_mark}{test_mark}\n"
        f"Дата формирования: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК\n"
        f"{'=' * 50}\n\n"
        f"РЕЗУЛЬТАТ: РАССЫЛКА ВРАЧАМ ОТПРАВЛЕНА\n\n"
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
        f"ДЕТАЛИЗАЦИЯ ПО ВРАЧАМ:\n"
        + "\n".join(per_doctor_lines) + "\n\n"
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
