"""SMTP и текст писем (компактная печатная форма под A4)."""
import os
import ssl
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import *
from utils import now_moscow

# Короткие названия заданий для компактного листа
TASK_SHORT = {
    1: "липиды",
    2: "КДП выгрузка",
    3: "КДП СД",
}

# Хвостик после каждой строки задания
TASK_SUFFIX = "   Выполнено: ДА    /  нет, виноват :(."

# Шуточные строки подвала (первая — до номера листа, вторая — после)
FOOTER_LINE_1 = "Лист сохраните, вдруг когда-нибудь понадобится, покажете внукам."
FOOTER_LINE_2 = "Царь молодец!"


def _short_text(task):
    """Убирает повторы из текста задания: 'провести КДП с диагнозом: X' → 'диагноз X'."""
    text = (task.get("text") or "").strip()
    low = text.lower()
    if low.startswith("провести кдп с диагнозом:"):
        return ("диагноз " + text[len("провести кдп с диагнозом:"):]).strip()
    if low in ("провести кдп", "провести кдп."):
        return ""
    return text


def build_email_body(doctor, patients, bot_id, target_date, branch=""):
    """Компактный лист заданий, удобный для печати на A4."""
    wide = "=" * 44
    thin = "-" * 44
    total_tasks = sum(len(p["tasks"]) for p in patients)

    lines = [
        wide,
        f"  ЛИСТ ЗАДАНИЙ НА {target_date.strftime('%d.%m.%Y')}",
    ]
    if branch:
        lines.append(f"  Филиал: {branch}")
    lines += [
        wide,
        f"Врач: {doctor['name']}",
        f"Пациентов: {len(patients)} | Заданий: {total_tasks}",
        thin,
        "",
    ]

    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        lines.append(f"{pnum}. {p_name} — ОМС {p_id}")

        tasks = patient["tasks"]
        multi = len(tasks) > 1
        for t_idx, task in enumerate(tasks):
            letter = f"{TASK_LETTERS[t_idx]}) " if multi else ""
            short = TASK_SHORT.get(task["task_number"], task["title"])
            text = _short_text(task)
            if text:
                base = f"   [ ] {letter}{short}: {text}"
            else:
                base = f"   [ ] {letter}{short}"
            lines.append(base + TASK_SUFFIX)
        lines.append("")

    lines += [
        thin,
        FOOTER_LINE_1,
        f"Лист № {bot_id} | {now_moscow().strftime('%d.%m.%Y %H:%M')}.",
        FOOTER_LINE_2,
        wide,
    ]
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
