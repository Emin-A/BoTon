"""Плановая рассылка заданий врачам."""
import os
from time import sleep
from config import *
from utils import normalize_email, now_moscow, get_signature
from tasks import group_by_doctor, build_patients, build_response_task_map, make_bot_id
from doctors import find_doctor_robust
from log_store import load_log_records, get_send_state, append_log_entries
from google_sheets import batch_update_cells
from mailer import build_email_body, send_email


def make_comment_value(email_addr, is_reserve):
    """Текст отметки в колонке «Комментарии»."""
    ts = now_moscow().strftime("%d.%m.%Y %H:%M")
    if is_reserve:
        return f"Отправлено на резервную почту {ts}"
    return f"Отправлено {email_addr} {ts}"


def send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date, cols):
    print("\n----- ОТПРАВКА ПИСЕМ ВРАЧАМ -----")
    if not rows:
        print("Нет строк для обработки.")
        return {
            "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
            "skipped": 0, "reserve": 0, "not_found": 0,
            "per_doctor": [],
        }

    grouped = group_by_doctor(rows)
    records = load_log_records(log_sheet)
    prepared = []
    skipped_logged = 0
    not_found = 0

    for _, doctor_rows in grouped.items():
        source_name = doctor_rows[0]["doctor_name"]
        doctor = find_doctor_robust(source_name, doctors)
        if not doctor:
            print(f"⚠️ Врач не найден в базе: {source_name}")
            not_found += 1
            continue

        patients = build_patients(doctor_rows)
        mapping = build_response_task_map(patients)
        sig = get_signature(doctor["name"])
        bot_id = make_bot_id(target_date, sig, doctor_rows)

        state = get_send_state(records, bot_id)
        if not TEST_TO_RESERVE:
            if state == "SENT" and not FORCE_RESEND:
                print(f"Пропуск (уже SENT): {bot_id}")
                skipped_logged += 1
                continue
            if state == "PENDING" and not FORCE_RESEND:
                print(f"Пропуск (PENDING): {bot_id}")
                skipped_logged += 1
                continue
            if state == "SENT" and FORCE_RESEND:
                print(f"ПОВТОРНАЯ ОТПРАВКА: {bot_id}")

        email_addr = normalize_email(doctor["email"])
        is_reserve = False
        if TEST_TO_RESERVE:
            is_reserve = True
        elif not email_addr:
            is_reserve = True

        resend_mark = " (ПОВТОРНО)" if FORCE_RESEND else ""
        test_mark = " [ТЕСТ]" if TEST_TO_RESERVE else ""
        branch = doctor_rows[0].get("branch", "")

        if TEST_TO_RESERVE:
            subject = f"[ТЕСТ ВРАЧА: {doctor['name']}] Задания на {target_date.strftime('%d.%m.%Y')}{test_mark}"
        else:
            subject = f"Задания на {target_date.strftime('%d.%m.%Y')}{resend_mark} [BOTON-ID:{bot_id}]"

        body = build_email_body(doctor, patients, bot_id, target_date, branch)

        prepared.append({
            "doctor": doctor,
            "doctor_rows": doctor_rows,
            "mapping": mapping,
            "bot_id": bot_id,
            "email": email_addr,
            "subject": subject,
            "body": body,
            "is_reserve": is_reserve,
            "task_count": sum(len(r["tasks"]) for r in doctor_rows),
            "patient_count": len(patients),
        })

    print(f"Подготовлено писем: {len(prepared)}")

    # ========================================================
    # ТЕСТОВЫЙ РЕЖИМ: ОДНО СВОДНОЕ ПИСЬМО НА ВСЕ РЕЗЕРВНЫЕ АДРЕСА
    # ========================================================
    if TEST_TO_RESERVE:
        if not prepared:
            print("Тестовый режим: нечего отправлять")
            return {
                "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
                "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
                "per_doctor": [],
            }

        print(f"\nТестовый режим: формирую ОДНО письмо со всеми {len(prepared)} письмами")
        parts = [
            f"ТЕСТОВЫЙ ПРОГОН BoTon v{VERSION}",
            f"Дата: {target_date.strftime('%d.%m.%Y')}",
            f"Врачей в тесте: {len(prepared)}",
            "Реальным врачам ничего не отправляется.",
            "",
            "=" * 60,
            "",
        ]
        total_tasks = 0
        per_doctor = []
        for idx, p in enumerate(prepared, start=1):
            tasks_n = p["task_count"]
            total_tasks += tasks_n
            per_doctor.append({
                "doctor": p["doctor"]["name"],
                "tasks": tasks_n,
                "patients": p["patient_count"],
            })
            parts.append(f"----- ПИСЬМО №{idx} ДЛЯ: {p['doctor']['name']} -----")
            parts.append(f"Кому (в боевом режиме): {p['doctor']['email'] or 'НЕТ EMAIL'}")
            parts.append(f"Заданий: {tasks_n} | Пациентов: {p['patient_count']}")
            parts.append(f"Тема: {p['subject']}")
            parts.append("")
            parts.append(p["body"])
            parts.append("")
            parts.append("=" * 60)
            parts.append("")

        combined_body = "\n".join(parts)
        combined_subject = (
            f"[ТЕСТ ВРАЧАМ] Сводка {len(prepared)} писем за "
            f"{target_date.strftime('%d.%m.%Y')} — BoTon v{VERSION}"
        )
        try:
            for reserve_addr in RESERVE_EMAILS:
                send_email(reserve_addr, combined_subject, combined_body)
                print(f"✅ ТЕСТОВОЕ СВОДНОЕ ПИСЬМО отправлено на {reserve_addr}")
            return {
                "doctors": len(prepared),
                "rows": sum(len(p["doctor_rows"]) for p in prepared),
                "tasks": total_tasks,
                "errors": 0,
                "skipped": skipped_logged,
                "reserve": len(prepared),
                "not_found": not_found,
                "per_doctor": per_doctor,
            }
        except Exception as exc:
            print(f"❌ ОШИБКА ТЕСТОВОЙ ОТПРАВКИ: {exc}")
            return {
                "doctors": 0, "rows": 0, "tasks": 0, "errors": 1,
                "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
                "per_doctor": [],
            }

    # ========================================================
    # БОЕВОЙ РЕЖИМ
    # ========================================================
    if not prepared:
        print("Нет писем для отправки.")
        return {
            "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
            "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
            "per_doctor": [],
        }

    pending = [{
        "event": "SEND",
        "bot_id": p["bot_id"],
        "target_date": target_date.strftime("%d.%m.%Y"),
        "doctor": p["doctor"]["name"],
        "email_address": p["email"] or ",".join(RESERVE_EMAILS),
        "mapping": p["mapping"],
        "result": "PENDING",
        "notes": "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else "",
    } for p in prepared]
    print(f"BoTon_Log: пакетная запись PENDING ({len(pending)})")
    append_log_entries(log_sheet, pending)

    sent_logs = []
    error_logs = []
    comment_updates = []
    successful_doctors = 0
    total_tasks = 0
    reserve_sent = 0
    errors = 0
    per_doctor = []

    comments_col = cols["comments"]

    smtp_from = os.environ.get("SMTP_FROM_EMAIL", "не задан")
    smtp_user = os.environ.get("SMTP_USER", "не задан")
    print("\n" + "=" * 60)
    print(" ДИАГНОСТИКА SMTP")
    print("=" * 60)
    print(f"  SMTP_USER:       {smtp_user}")
    print(f"  SMTP_FROM_EMAIL: {smtp_from}")
    print(f"  FROM == USER?    {smtp_from == smtp_user}")
    print(f"  Колонка комментариев: {comments_col + 1}")
    print("=" * 60 + "\n")

    for p in prepared:
        doctor = p["doctor"]
        bot_id = p["bot_id"]
        email_addr = p["email"]
        is_reserve = p["is_reserve"]

        try:
            print(f"📧 ОТПРАВКА: {doctor['name']} -> {email_addr or 'РЕЗЕРВ'}")

            if is_reserve:
                for reserve_addr in RESERVE_EMAILS:
                    send_email(reserve_addr, p["subject"], p["body"])
                    print(f"   ✅ Резерв: {reserve_addr}")
            else:
                send_email(email_addr, p["subject"], p["body"])
                print(f"   ✅ Отправлено")

            sent_logs.append({
                "event": "SEND",
                "bot_id": bot_id,
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": doctor["name"],
                "email_address": email_addr or ",".join(RESERVE_EMAILS),
                "mapping": p["mapping"],
                "result": "SENT",
                "notes": "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else "",
            })

            comment = make_comment_value(email_addr, is_reserve)
            for r in p["doctor_rows"]:
                comment_updates.append({
                    "row": r["row_number"],
                    "col": comments_col + 1,
                    "value": comment,
                })

            per_doctor.append({
                "doctor": doctor["name"],
                "tasks": p["task_count"],
                "patients": p["patient_count"],
            })

            successful_doctors += 1
            total_tasks += p["task_count"]
            if is_reserve:
                reserve_sent += 1
            sleep(2)

        except Exception as exc:
            errors += 1
            error_logs.append({
                "event": "SEND",
                "bot_id": bot_id,
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": doctor["name"],
                "email_address": email_addr or ",".join(RESERVE_EMAILS),
                "mapping": p["mapping"],
                "result": "ERROR",
                "notes": str(exc),
            })
            print(f"   ❌ Ошибка отправки: {exc}")

    if sent_logs or error_logs:
        print(f"BoTon_Log: пакетная запись SENT/ERROR ({len(sent_logs) + len(error_logs)})")
        append_log_entries(log_sheet, sent_logs + error_logs)

    if comment_updates:
        print(f"Таблица: запись отметок об отправке в колонку {comments_col + 1} ({len(comment_updates)} строк)")
        batch_update_cells(tasks_sheet, comment_updates, "запись комментариев")

    print("\nИТОГ РАССЫЛКИ:")
    print(f"  Врачей обработано: {successful_doctors}")
    print(f"  Успешно отправлено: {successful_doctors - errors}")
    print(f"  Отправлено на резерв: {reserve_sent}")
    print(f"  Пропущено по журналу: {skipped_logged}")
    print(f"  Врач не найден: {not_found}")
    print(f"  Ошибок: {errors}")
    print(f"  Строк обновлено в комментариях: {len(comment_updates)}")
    print(f"  Всего заданий: {total_tasks}")

    return {
        "doctors": successful_doctors,
        "rows": len(comment_updates),
        "tasks": total_tasks,
        "errors": errors,
        "skipped": skipped_logged,
        "reserve": reserve_sent,
        "not_found": not_found,
        "per_doctor": per_doctor,
    }
