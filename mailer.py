"""SMTP, текст писем и диагностическая отправка."""
import os, ssl, smtplib
from email.message import EmailMessage
from config import *
from utils import normalize_text, now_moscow, get_signature
from tasks import build_patients, make_bot_id, group_by_doctor
from doctors import find_doctor_robust
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
        "Вам назначены задания по пациентам, записанным на сегодня:",
        "",
    ]
    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        lines.append(f"{pnum}. {p_name} — ОМС/ID: {p_id}")
        for t_idx, task in enumerate(patient["tasks"]):
            prefix = "" if len(patient["tasks"]) == 1 else f"{TASK_LETTERS[t_idx]}) "
            lines.append(f"   {prefix}{task['title']}")
            if task["text"]:
                lines.append(f"      {task['text']}")
        lines.append("")

    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    lines.extend([
        f"BOTON-ID: {bot_id}{resend_mark}",
        "",
        "Просьба вернуть отработанные задания заведующему отделением.",
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

def send_doctor_diagnostic(rows, doctors, target_date):
    print("\n" + "=" * 60)
    print("ДИАГНОСТИКА: ОДНО ПИСЬМО ЛЮБОГО ВРАЧА → TEST_EMAIL")
    print("=" * 60)
    print(f"Получатель диагностики: {TEST_EMAIL}")

    if not rows:
        print("Нет подходящих строк для диагностики (см. статистику выше).")
        return False

    grouped = group_by_doctor(rows)
    doctor_key, doctor_rows = next(iter(grouped.items()))
    source_name = doctor_rows[0]["doctor_name"]
    doctor = find_doctor_robust(source_name, doctors)

    if not doctor:
        print(f"Врач '{source_name}' не найден в базе — диагностика невозможна.")
        return False

    patients = build_patients(doctor_rows)
    sig = get_signature(doctor["name"])
    bot_id = make_bot_id(target_date, sig, doctor_rows)
    body = build_email_body(doctor, patients, bot_id)

    print("\n--- ИНФОРМАЦИЯ О ПИСЬМЕ ---")
    print(f"Врач (из таблицы):  {source_name}")
    print(f"Врач (из базы):     {doctor['name']}")
    print(f"Email врача:        {doctor['email'] or 'НЕТ'}")
    print(f"Строк у врача:      {len(doctor_rows)}")
    print(f"Заданий:            {sum(len(r['tasks']) for r in doctor_rows)}")
    print(f"BOTON-ID:           {bot_id}")
    print(f"Дата:               {target_date.strftime('%d.%m.%Y')}")
    print("--- ТЕЛО ПИСЬМА ---")
    print(body)
    print("--- КОНЕЦ ТЕЛА ---\n")

    subject = (
        f"[ДИАГНОСТИКА BoTon] Для врача: {doctor['name']} — "
        f"{target_date.strftime('%d.%m.%Y')}"
    )

    try:
        send_email(TEST_EMAIL, subject, body)
        print("✅ ДИАГНОСТИЧЕСКОЕ ПИСЬМО ОТПРАВЛЕНО")
        print(f"   Получатель: {TEST_EMAIL}")
        print(f"   Тема: {subject}")
        return True
    except Exception as exc:
        print(f"❌ ОШИБКА ДИАГНОСТИЧЕСКОЙ ОТПРАВКИ: {exc}")
        return False