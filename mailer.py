"""SMTP, текст писем и диагностическая отправка."""
import os, ssl, smtplib
from email.message import EmailMessage
from config import *
from utils import normalize_text, now_moscow, get_signature
from tasks import build_patients, make_bot_id, group_by_doctor, build_response_task_map
from doctors import find_doctor_robust
from log_store import append_log_entries
def build_email_body(doctor, patients, bot_id):
    normalized = normalize_text(doctor["name"])
    parts = normalized.split()
    first = parts[1].capitalize() if len(parts) >= 2 else ""
    patr = parts[2].capitalize() if len(parts) >= 3 else ""
    if first and patr:
        address = f"{first} {patr}"
    elif first:
        address = first
    else:
        address = doctor["name"]

    lines = [
        f"Здравствуйте, {address}!",
        "",
        "Вам назначены задания по пациентам, записанным на сегодня.",
        "",
        "КАК ОТВЕТИТЬ В КОНЦЕ СМЕНЫ:",
        "1) Нажмите «Ответить» (текст письма с квадратиками должен остаться).",
        "2) Там, где задание сделано, замените пустой квадратик на отмеченный.",
        "   Было: [ ]     Стало: [x]",
        "3) Если консультация не состоялась, отметьте строку «консультация не состоялась».",
        "4) Что не отмечено, считается не отработанным.",
        "5) Не удаляйте из письма номера пациентов и строку BOTON-ID.",
        "",
        "=" * 50,
        "",
    ]
    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        tasks = patient["tasks"]
        multi = len(tasks) > 1
        lines.append(f"{pnum}. {p_name} — ОМС/ID: {p_id}")
        for t_idx, task in enumerate(tasks):
            key = f"{pnum}{TASK_LETTERS[t_idx]}" if multi else f"{pnum}"
            prefix = f"{TASK_LETTERS[t_idx]}) " if multi else ""
            lines.append(f"   {prefix}{task['title']}")
            if task["text"]:
                lines.append(f"      {task['text']}")
            lines.append(f"      [ ] {key} — отработано")
            lines.append(f"      [ ] {key} — консультация не состоялась")
        if multi:
            lines.append(f"   [ ] {pnum} — все задания отработаны")
        lines.append("")

    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    lines.extend([
        "=" * 50,
        f"BOTON-ID: {bot_id}{resend_mark}",
        "",
        f"Отправлено: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК",
    ])
    return "\n".join(lines)

def send_email(to_email, subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)

    if not to_email:
        raise RuntimeError("Пустой получатель")

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, subtype="plain", charset="utf-8")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)

def send_doctor_diagnostic(rows, doctors, target_date, log_sheet=None):
    """Тест: письмо одного врача уходит на TEST_EMAIL (ФИО и ОМС пациентов заменены).

    Письму присваивается особый код BOTON-TEST-..., он записывается в журнал,
    чтобы ответ на письмо можно было разобрать. Реальную рассылку это не затрагивает:
    столбец M не заполняется, а код отличается от боевого.
    """
    print("\n" + "=" * 60)
    print("ТЕСТ: ПИСЬМО ОДНОГО ВРАЧА → TEST_EMAIL")
    print("=" * 60)
    print(f"Получатель теста: {TEST_EMAIL}")

    if not rows:
        print("Нет подходящих строк для теста (см. статистику выше).")
        return False

    doctor, doctor_rows = None, None
    for _, candidate_rows in group_by_doctor(rows).items():
        found = find_doctor_robust(candidate_rows[0]["doctor_name"], doctors)
        if found:
            doctor, doctor_rows = found, candidate_rows
            break

    if not doctor:
        print("Ни один врач из строк не найден в базе — тест невозможен.")
        return False

    # Анонимизация: в тестовом письме нет настоящих ФИО и номеров ОМС.
    patients = []
    for idx, p in enumerate(build_patients(doctor_rows), start=1):
        p = dict(p)
        p["patient_name"] = f"Тестовый пациент {idx}"
        p["patient_id"] = "000000"
        patients.append(p)

    mapping = build_response_task_map(patients)
    sig = get_signature(doctor["name"])
    real_id = make_bot_id(target_date, sig, doctor_rows)
    bot_id = f"BOTON-TEST-{real_id[len('BOTON-'):]}-{now_moscow().strftime('%H%M%S')}"

    header = "\n".join([
        "=== ТЕСТ BoTon ===",
        f"Это письмо, которое получил бы врач: {doctor['name']}.",
        "ФИО пациентов и номера ОМС заменены на тестовые.",
        "Ответьте на него так, как ответил бы врач (замените [ ] на [x]).",
        "Результат разбора придёт вам отдельным письмом (до 30 минут).",
        "==================",
        "",
        "",
    ])
    body = header + build_email_body(doctor, patients, bot_id)

    # Тело письма в лог НЕ печатается.
    print("\n--- ИНФОРМАЦИЯ О ПИСЬМЕ ---")
    print(f"Врач:               {doctor['name']}")
    print(f"Пациентов в письме: {len(patients)}")
    print(f"Заданий:            {sum(len(p['tasks']) for p in patients)}")
    print(f"BOTON-ID:           {bot_id}")
    print(f"Дата:               {target_date.strftime('%d.%m.%Y')}")

    subject = (
        f"[ТЕСТ BoTon] Задания врача {doctor['name']} за "
        f"{target_date.strftime('%d.%m.%Y')} [BOTON-ID:{bot_id}]"
    )

    try:
        send_email(TEST_EMAIL, subject, body)
    except Exception as exc:
        print(f"❌ ОШИБКА ТЕСТОВОЙ ОТПРАВКИ: {exc}")
        return False

    print("✅ ТЕСТОВОЕ ПИСЬМО ОТПРАВЛЕНО")
    if log_sheet is not None:
        try:
            append_log_entries(log_sheet, [{
                "event": "SEND",
                "bot_id": bot_id,
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": doctor["name"],
                "email_address": TEST_EMAIL,
                "mapping": mapping,
                "result": "SENT",
                "notes": "ТЕСТ",
            }])
        except Exception as exc:
            print(f"⚠️ Не удалось записать тест в журнал: {exc}")
            return False
    return True
