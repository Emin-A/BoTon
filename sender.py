"""Отправка писем врачам с заданиями."""
import os
from config import *
from utils import now_moscow, get_signature
from tasks import build_patients, make_bot_id, group_by_doctor, build_response_task_map
from doctors import find_doctor_robust
from google_sheets import batch_update_cells
from mailer import (
    send_email, 
    build_email_body, 
    build_email_body_html, 
    build_doctor_form_html,
)
from log_store import append_log_entries


def send_doctor_emails(rows, doctors, tasks_sheet, log_sheet, target_date):
    """Основная функция отправки писем врачам."""
    print("\n----- ОТПРАВКА ПИСЕМ ВРАЧАМ -----")
    if not rows:
        print("Нет строк для обработки.")
        return

    grouped = group_by_doctor(rows)
    print(f"Найдено врачей для отправки: {len(grouped)}")

    prepared = []
    for doctor_name, doctor_rows in grouped.items():
        doctor = find_doctor_robust(doctor_name, doctors)
        if not doctor:
            print(f"⚠️ Врач '{doctor_name}' не найден в базе. Пропуск.")
            continue
        
        email_addr = doctor.get("email", "").strip()
        is_reserve = False
        
        # Тестовый режим: отправка на резервный адрес
        if TEST_TO_RESERVE:
            email_addr = TEST_EMAIL
            is_reserve = True
        elif not email_addr:
            print(f"️ У врача '{doctor_name}' нет email. Пропуск.")
            continue

        patients = build_patients(doctor_rows)
        mapping = build_response_task_map(patients)
        sig = get_signature(doctor_name)
        bot_id = make_bot_id(target_date, sig, doctor_rows)
        
        # Тема письма (изменена по вашему запросу)
        subject = f"Задания для пациентов на {target_date.strftime('%d.%m.%Y')} [BOTON-ID:{bot_id}]"
        
        # Формируем все версии письма
        body = build_email_body(doctor, patients, bot_id)
        html_body = build_email_body_html(doctor, patients, bot_id)
        form_html = build_doctor_form_html(doctor, patients, bot_id, target_date)

        # Собираем вложения (офлайн-форма)
        attachments = []
        if form_html:
            form_filename = f"otchet_{target_date.strftime('%d.%m.%Y')}.html"
            attachments.append((form_filename, form_html))

        prepared.append({
            "doctor": doctor,
            "doctor_rows": doctor_rows,
            "mapping": mapping,
            "bot_id": bot_id,
            "email": email_addr,
            "subject": subject,
            "body": body,
            "html_body": html_body,
            "attachments": attachments,
            "is_reserve": is_reserve,
        })

    print(f"Подготовлено писем: {len(prepared)}")
    
    send_logs = []
    
    for p in prepared:
        try:
            # Отправка письма с HTML и вложениями
            send_email(
                p["email"], 
                p["subject"], 
                p["body"], 
                p.get("html_body"), 
                p.get("attachments")
            )
            print(f"✅ Отправлено: {p['doctor']['name']} -> {p['email']}")
            
            send_logs.append({
                "event": "SEND",
                "bot_id": p["bot_id"],
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": p["doctor"]["name"],
                "email_address": p["email"],
                "mapping": p["mapping"],
                "message_id": "",
                "result": "SENT",
                "notes": "ПОВТОРНО" if FORCE_RESEND else "",
            })
            
        except Exception as exc:
            print(f"❌ Ошибка отправки {p['doctor']['name']}: {exc}")
            send_logs.append({
                "event": "SEND",
                "bot_id": p["bot_id"],
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": p["doctor"]["name"],
                "email_address": p["email"],
                "mapping": p["mapping"],
                "message_id": "",
                "result": "ERROR",
                "notes": str(exc),
            })

    if send_logs:
        print(f"BoTon_Log: пакетная запись SEND ({len(send_logs)})")
        append_log_entries(log_sheet, send_logs)
