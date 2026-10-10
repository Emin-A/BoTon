"""SMTP, текст писем и диагностическая отправка."""
import os
import ssl
import smtplib
import json
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email import encoders
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


def build_email_body_html(doctor, patients, bot_id):
    """HTML-версия письма с кнопками для быстрого ответа."""
    address = _get_address_name(doctor)
    resend_mark = " (ПОВТОРНАЯ ОТПРАВКА)" if FORCE_RESEND else ""
    from_email = os.environ.get("SMTP_FROM_EMAIL", os.environ.get("SMTP_USER", ""))
    today_str = now_moscow().strftime("%d.%m.%Y")

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2c3e50;">Здравствуйте, {address}!</h2>
        <p>Вам назначены задания по пациентам, записанным на сегодня.</p>

        <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <h3 style="margin-top: 0; color: #34495e;">Как ответить в конце смены:</h3>
            <ol style="color: #555;">
                <li>Нажмите на кнопку под заданием (или используйте «Ответить» в почте).</li>
                <li>Если задание выполнено — нажмите «✓ Отработано».</li>
                <li>Если консультация не состоялась — нажмите «✗ Не состоялась».</li>
                <li>Что не отмечено, считается не отработанным.</li>
            </ol>
        </div>

        <hr style="border: none; border-top: 2px solid #3498db; margin: 30px 0;">
    """

    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        tasks = patient["tasks"]
        multi = len(tasks) > 1

        html += f"""
        <div style="background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin: 15px 0;">
            <h3 style="color: #2c3e50; margin-top: 0;">{pnum}. {p_name} — ОМС/ID: {p_id}</h3>
        """

        for t_idx, task in enumerate(tasks):
            key = f"{pnum}{TASK_LETTERS[t_idx]}" if multi else f"{pnum}"
            prefix = f"{TASK_LETTERS[t_idx]}) " if multi else ""
            task_title = task["title"]
            task_text = task.get("text", "")

            html += f"""
            <div style="margin: 15px 0; padding: 10px; background: #f8f9fa; border-radius: 5px;">
                <p style="margin: 0 0 5px 0;"><strong>{prefix}{task_title}</strong></p>
                {f'<p style="margin: 0 0 10px 0; color: #666;">{task_text}</p>' if task_text else ''}
                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    <a href="mailto:{from_email}?subject=Re%3A%20Задания%20для%20пациентов%20на%20{today_str}%20%5BBOTON-ID%3A{bot_id}%5D&body=%5Bx%5D%20{key}%20%E2%80%94%20%D0%BE%D1%82%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%BD%D0%BE%0D%0A"
                       style="background: #27ae60; color: white; padding: 8px 15px; text-decoration: none; border-radius: 5px; font-size: 14px;">
                        ✓ Отработано
                    </a>
                    <a href="mailto:{from_email}?subject=Re%3A%20Задания%20для%20пациентов%20на%20{today_str}%20%5BBOTON-ID%3A{bot_id}%5D&body=%5Bx%5D%20{key}%20%E2%80%94%20%D0%BA%D0%BE%D0%BD%D1%81%D1%83%D0%BB%D1%8C%D1%82%D0%B0%D1%86%D0%B8%D1%8F%20%D0%BD%D0%B5%20%D1%81%D0%BE%D1%81%D1%82%D0%BE%D1%8F%D0%BB%D0%B0%D1%81%D1%8C%0D%0A"
                       style="background: #e74c3c; color: white; padding: 8px 15px; text-decoration: none; border-radius: 5px; font-size: 14px;">
                        ✗ Не состоялась
                    </a>
                </div>
            </div>
            """

        if multi:
            html += f"""
            <div style="margin: 15px 0; padding: 10px; background: #f8f9fa; border-radius: 5px;">
                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    <a href="mailto:{from_email}?subject=Re%3A%20Задания%20для%20пациентов%20на%20{today_str}%20%5BBOTON-ID%3A{bot_id}%5D&body=%5Bx%5D%20{pnum}%20%E2%80%94%20%D0%B2%D1%81%D0%B5%20%D0%B7%D0%B0%D0%B4%D0%B0%D0%BD%D0%B8%D1%8F%20%D0%BE%D1%82%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%BD%D1%8B%0D%0A"
                       style="background: #3498db; color: white; padding: 8px 15px; text-decoration: none; border-radius: 5px; font-size: 14px;">
                        ✓ Все задания отработаны
                    </a>
                </div>
            </div>
            """

        html += "</div>"

    html += f"""
        <hr style="border: none; border-top: 2px solid #3498db; margin: 30px 0;">
        <div style="background: #ecf0f1; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <p style="margin: 0; color: #555; font-size: 12px;">
                <strong>BOTON-ID:</strong> {bot_id}{resend_mark}<br>
                <strong>Отправлено:</strong> {now_moscow().strftime('%d.%m.%Y %H:%M')} МСК
            </p>
        </div>

        <div style="text-align: center; margin: 30px 0;">
            <a href="mailto:{from_email}?subject=Re%3A%20Задания%20для%20пациентов%20на%20{today_str}%20%5BBOTON-ID%3A{bot_id}%5D&body=%D0%97%D0%B4%D1%80%D0%B0%D0%B2%D1%81%D1%82%D0%B2%D1%83%D0%B9%D1%82%D0%B5!%0D%0A%0D%0A%5B%D0%97%D0%B4%D0%B5%D1%81%D1%8C%20%D0%BE%D1%82%D0%BC%D0%B5%D1%82%D1%8C%D1%82%D0%B5%20%D0%B7%D0%B0%D0%B4%D0%B0%D0%BD%D0%B8%D1%8F%5D%0D%0A%0D%0ABOTON-ID%3A%20{bot_id}"
               style="background: #3498db; color: white; padding: 12px 25px; text-decoration: none; border-radius: 8px; font-size: 16px; display: inline-block;">
                ✍️ Сформировать ответ вручную
            </a>
        </div>
    </div>
    """

    return html


def build_doctor_form_html(doctor, patients, bot_id, target_date):
    """Генерирует персональный HTML-файл с формой для врача (офлайн)."""
    template_path = os.path.join(os.path.dirname(__file__), "form_template.html")

    # Если шаблона нет — возвращаем None, форма не будет приложена
    if not os.path.exists(template_path):
        return None

    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    # Формируем данные пациентов
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

    # Заменяем плейсхолдеры
    html = template.replace("{{BOTON_ID}}", bot_id)
    html = html.replace("{{DOCTOR_NAME}}", doctor["name"])
    html = html.replace("{{DATE}}", target_date.strftime("%d.%m.%Y"))
    html = html.replace("{{PATIENTS_JSON}}", json.dumps(patients_data, ensure_ascii=False))

    return html


def send_email(to_email, subject, body, html_body=None, attachments=None):
    """Отправка письма. Поддерживает HTML и вложения."""
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

    if html_body:
        msg.set_content(body, subtype="plain", charset="utf-8")
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    else:
        msg.set_content(body, subtype="plain", charset="utf-8")

    # Добавляем вложения
    if attachments:
        for filename, content in attachments:
            msg.add_attachment(
                content,
                maintype="text",
                subtype="html",
                filename=filename,
                charset="utf-8",
            )

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
    html_body = build_email_body_html(doctor, patients, bot_id)

    print("\n--- ИНФОРМАЦИЯ О ПИСЬМЕ ---")
    print(f"Врач:               {doctor['name']}")
    print(f"Пациентов в письме: {len(patients)}")
    print(f"Заданий:            {sum(len(p['tasks']) for p in patients)}")
    print(f"BOTON-ID:           {bot_id}")
    print(f"Дата:               {target_date.strftime('%d.%m.%Y')}")

    subject = (
        f"[ТЕСТ BoTon] Задания для пациентов на "
        f"{target_date.strftime('%d.%m.%Y')} [BOTON-ID:{bot_id}]"
    )

    try:
        send_email(TEST_EMAIL, subject, body, html_body)
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
            print(f"️ Не удалось записать тест в журнал: {exc}")
            return False

    return True
