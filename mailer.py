"""SMTP и текст писем (печатная форма под A4)."""
import os
import ssl
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import *
from utils import normalize_text, now_moscow


def _get_address_name(doctor):
    """Возвращает обращение к врачу: 'Имя Отчество'."""
    normalized = normalize_text(doctor["name"])
    parts = normalized.split()
    first = parts[1].capitalize() if len(parts) >= 2 else ""
    patr = parts[2].capitalize() if len(parts) >= 3 else ""
    if first and patr:
        return f"{first} {patr}"
    elif first:
        return first
    return doctor["name"]


def build_email_body(doctor, patients, bot_id, target_date, branch=""):
    """Письмо-лист заданий, удобное для печати на A4."""
    address = _get_address_name(doctor)
    wide = "=" * 46
    thin = "-" * 46

    lines = [
        wide,
        "          ЛИСТ ЗАДАНИЙ НА СМЕНУ",
        f"          Дата: {target_date.strftime('%d.%m.%Y')}",
        wide,
        f"Врач: {doctor['name']}",
        f"Обращение: {address}",
    ]
    if branch:
        lines.append(f"Филиал: {branch}")
    lines.append(f"Пациентов в работе: {len(patients)}")
    lines.append(thin)
    lines.append("")

    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        tasks = patient["tasks"]
        multi = len(tasks) > 1

        lines.append(f"ПАЦИЕНТ {pnum}. {p_name}")
        lines.append(f"   ОМС/ID: {p_id}")
        for t_idx, task in enumerate(tasks):
            prefix = f"{TASK_LETTERS[t_idx]}) " if multi else ""
            lines.append(f"   [ ] {prefix}{task['title']}")
            if task["text"]:
                lines.append(f"       {task['text']}")
        lines.append("")

    lines.append(thin)
    lines.append("КАК РАБОТАТЬ С ЛИСТОМ:")
    lines.append("  1. Распечатайте этот лист на A4 или держите открытым на экране.")
    lines.append("  2. В течение смены отмечайте выполненные задания ручкой")
    lines.append("     в квадратиках [ ].")
    lines.append("  3. В конце смены передайте лист заведующему отделением.")
    lines.append(thin)
    lines.append(f"Номер листа: {bot_id}")
    lines.append(f"Сформировано: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК")
    lines.append(wide)
    return "\n".join(lines)


def send_email(to_email, subject, body):
    """Отправка plain-text письма."""
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)

    if not to_email:
        raise RuntimeError("Пустой получатель")
    if not host or not user or not password:
        raise RuntimeError("Не настроены SMTP_HOST/SMTP_USER/SMTP_PASSWORD")

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
