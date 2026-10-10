"""SMTP, текст писем и диагностическая отправка."""
import os
import ssl
import smtplib
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import *
from utils import normalize_text, now_moscow, get_signature
from tasks import build_patients, make_bot_id, group_by_doctor, build_response_task_map
from doctors import find_doctor_robust
from log_store import append_log_entries


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


def build_email_body(doctor, patients, bot_id):
    """Plain-text версия письма."""
    address = _get_address_name(doctor)
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""

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
        "ИЛИ ЗАПОЛНИТЕ УДОБНУЮ ФОРМУ (приложена к письму):",
        "Откройте файл отчёт.xhtml в браузере, нажмите кнопки и отправьте ответ.",
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

    lines.extend([
        "=" * 50,
        f"BOTON-ID: {bot_id}{resend_mark}",
        "",
        f"Отправлено: {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК",
    ])
    return "\n".join(lines)


def build_doctor_form_html(doctor, patients, bot_id, target_date, to_email):
    """Генерирует персональный XHTML-файл с формой для врача (офлайн)."""
    template_path = os.path.join(os.path.dirname(__file__), "form_template.html")

    if not os.path.exists(template_path):
        return None

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    patients_data = []
    for pnum, patient in enumerate(patients, start=1):
        tasks = []
        for t_idx, task in enumerate(patient["tasks"]):
            key = f"{pnum}{TASK_LETTERS[t_idx]}" if len(patient["tasks"]) > 1 else f"{pnum}"
            tasks.append({
                "key": key,
                "title": task["title"],
                "letter": TASK_LETTERS[t_idx] if len(patient["tasks"]) > 1 else "",
            })
        patients_data.append({
            "number": pnum,
            "name": patient["patient_name"] or "ФИО не указано",
            "id": patient["patient_id"] or "не указан",
            "tasks": tasks,
        })

    html = template.replace("{{BOTON_ID}}", bot_id)
    html = html.replace("{{DOCTOR_NAME}}", doctor["name"])
    html = html.replace("{{DATE}}", target_date.strftime("%d.%m.%Y"))
    html = html.replace("{{TO_EMAIL}}", to_email)
    html = html.replace("{{PATIENTS_JSON}}", json.dumps(patients_data, ensure_ascii=False))

    return html


def send_email(to_email, subject, body, attachments=None):
    """Отправка письма. Поддерживает вложения (XHTML-формы)."""
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

    if attachments:
        for filename, content in attachments:
            part = MIMEText(content, "html", "utf-8")
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def send_doctor_diagnostic(rows, doctors, target_date, log_sheet=None):
    """Тест: письмо одного врача уходит на TEST_EMAIL."""
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

    form_html = build_doctor_form_html(
        doctor, patients, bot_id, target_date, TEST_EMAIL
    )
    attachments = []
    if form_html:
        form_filename = f"otchet_{target_date.strftime('%d.%m.%Y')}.xhtml"
        attachments.append((form_filename, form_html))

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
        send_email(TEST_EMAIL, subject, body, attachments)
    except Exception as exc:
        print(f"❌ ОШИБКА ТЕСТОВОЙ ОТПРАВКИ: {exc}")
        return False

    print("✅ ТЕСТОВОЕ ПИСЬМО ОТПРАВЛЕНО (с формой)")

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
