"""Плановая рассылка заданий врачам."""
import os
from time import sleep
from config import *
from utils import normalize_email, now_moscow, get_signature
from tasks import group_by_doctor, build_patients, build_response_task_map, make_bot_id
from doctors import find_doctor_robust
from log_store import load_log_records, get_send_state, append_log_entries
from google_sheets import batch_update_cells
from mailer import build_email_body, build_doctor_form_html, send_email


def make_status_value(email_addr, is_resend, is_test, is_reserve):
    ts = now_moscow().strftime("%d.%m.%Y %H:%M")
    marks = []
    if is_test:
        marks.append("ТЕСТ")
    if is_resend:
        marks.append("ПОВТОРНО")
    mark = f" ({', '.join(marks)})" if marks else ""
    if is_reserve:
        return f"Отправлено{mark} на резервную почту {ts}"
    return f"Отправлено{mark} на {email_addr} {ts}"


def send_today_assignments(tasks_sheet, log_sheet, doctors, rows, target_date):
    print("\n----- ОТПРАВКА ПИСЕМ ВРАЧАМ -----")
    if not rows:
        print("Нет строк для обработки.")
        return {
            "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
            "skipped": 0, "reserve": 0, "not_found": 0,
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

        if TEST_TO_RESERVE:
            subject = f"[ТЕСТ ВРАЧА: {doctor['name']}] Задания для пациентов на {target_date.strftime('%d.%m.%Y')}{test_mark}"
        else:
            subject = f"Задания для пациентов на {target_date.strftime('%d.%m.%Y')}{resend_mark} [BOTON-ID:{bot_id}]"

        body = build_email_body(doctor, patients, bot_id)

        form_html = build_doctor_form_html(
            doctor, patients, bot_id, target_date, email_addr or RESERVE_EMAILS[0]
        )
        attachments = []
        if form_html:
            form_filename = f"otchet_{target_date.strftime('%d.%m.%Y')}.xhtml"
            attachments.append((form_filename, form_html))

        prepared.append({
            "doctor": doctor,
            "doctor_rows": doctor_rows,
            "mapping": mapping,
            "bot_id": bot_id,
            "email": email_addr,
            "subject": subject,
            "body": body,
            "attachments": attachments,
            "is_reserve": is_reserve,
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
            }

        print(f"\nТестовый режим: формирую ОДНО письмо со всеми {len(prepared)} письмами врачам")
        parts = [
            f"ТЕСТОВЫЙ ПРОГОН BoTon v{VERSION}",
            f"Дата: {target_date.strftime('%d.%m.%Y')}",
            f"Врачей в тесте: {len(prepared)}",
            "Все задания идут на резервную почту, реальным врачам не отправляются.",
            "",
            "К каждому врачу приложена отдельная XHTML-форма (otchet_*.xhtml).",
            "Откройте форму в браузере, отметьте задания и нажмите «Отправить».",
            "",
            "=" * 60,
            "",
        ]
        total_tasks = 0
        all_attachments = []
        for idx, p in enumerate(prepared, start=1):
            total_tasks += sum(len(r["tasks"]) for r in p["doctor_rows"])
            parts.append(f"----- ПИСЬМО №{idx} ДЛЯ: {p['doctor']['name']} -----")
            parts.append(f"Кому (в боевом режиме): {p['doctor']['email'] or 'НЕТ EMAIL'}")
            parts.append(f"Тема: {p['subject']}")
            parts.append("")
            parts.append(p["body"])
            parts.append("")
            parts.append("=" * 60)
            parts.append("")
            if p["attachments"]:
                all_attachments.extend(p["attachments"])

        combined_body = "\n".join(parts)
        combined_subject = (
            f"[ТЕСТ ВРАЧАМ] Сводка {len(prepared)} писем за "
            f"{target_date.strftime('%d.%m.%Y')} — BoTon v{VERSION}"
        )
        try:
            for reserve_addr in RESERVE_EMAILS:
                send_email(reserve_addr, combined_subject, combined_body, all_attachments)
                print(f"✅ ТЕСТОВОЕ СВОДНОЕ ПИСЬМО отправлено на {reserve_addr}")
            print(f"   Содержит {len(prepared)} писем врачам и {len(all_attachments)} форм")
            return {
                "doctors": len(prepared),
                "rows": sum(len(p["doctor_rows"]) for p in prepared),
                "tasks": total_tasks,
                "errors": 0,
                "skipped": skipped_logged,
                "reserve": len(prepared),
                "not_found": not_found,
            }
        except Exception as exc:
            print(f"❌ ОШИБКА ТЕСТОВОЙ ОТПРАВКИ: {exc}")
            return {
                "doctors": 0, "rows": 0, "tasks": 0, "errors": 1,
                "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
            }

    # ========================================================
    # БОЕВОЙ РЕЖИМ
    # ========================================================
    if not prepared:
        print("Нет писем для отправки.")
        return {
            "doctors": 0, "rows": 0, "tasks": 0, "errors": 0,
            "skipped": skipped_logged, "reserve": 0, "not_found": not_found,
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
    n_updates = []
    successful_doctors = 0
    total_tasks = 0
    reserve_sent = 0
    errors = 0

    # === ГЛОБАЛЬНАЯ ДИАГНОСТИКА SMTP (один раз в начале) ===
    smtp_host = os.environ.get("SMTP_HOST", "не задан")
    smtp_user = os.environ.get("SMTP_USER", "не задан")
    smtp_from = os.environ.get("SMTP_FROM_EMAIL", "не задан")
    smtp_port = os.environ.get("SMTP_PORT", "не задан")
    from_matches_user = (smtp_from == smtp_user)

    print("\n" + "=" * 60)
    print(" ДИАГНОСТИКА SMTP-НАСТРОЕК")
    print("=" * 60)
    print(f"  SMTP_HOST:           {smtp_host}")
    print(f"  SMTP_PORT:           {smtp_port}")
    print(f"  SMTP_USER (логин):   {smtp_user}")
    print(f"  SMTP_FROM_EMAIL:     {smtp_from}")
    print(f"  FROM == USER?        {from_matches_user}")
    if not from_matches_user:
        print(f"  ⚠️ ВНИМАНИЕ: Адрес отправителя (FROM) НЕ совпадает с логином (USER)!")
        print(f"     Яндекс может молча отбрасывать такие письма.")
        print(f"     Исправьте секрет SMTP_FROM_EMAIL в GitHub Settings → Secrets.")
        print(f"     Он должен быть равен: {smtp_user}")
    print("=" * 60 + "\n")
    # === КОНЕЦ ГЛОБАЛЬНОЙ ДИАГНОСТИКИ ===

    for p in prepared:
        doctor = p["doctor"]
        bot_id = p["bot_id"]
        email_addr = p["email"]
        is_reserve = p["is_reserve"]

        try:
            # === ДИАГНОСТИКА КАЖДОГО ПИСЬМА ===
            print(f"\n📧 ОТПРАВКА: {doctor['name']}")
            print(f"  FROM (от кого):      {smtp_from}")
            print(f"  TO (кому):           {email_addr}")
            print(f"  SUBJECT (тема):      {p['subject']}")
            print(f"  Вложений:            {len(p.get('attachments', []))}")
            if not from_matches_user:
                print(f"  ⚠️ FROM != SMTP_USER — письмо может не дойти!")
            print()
            # === КОНЕЦ ДИАГНОСТИКИ ===

            if is_reserve:
                for reserve_addr in RESERVE_EMAILS:
                    send_email(reserve_addr, p["subject"], p["body"], p["attachments"])
                    print(f"✅ Резерв: {doctor['name']} -> {reserve_addr}")
            else:
                send_email(email_addr, p["subject"], p["body"], p["attachments"])
                print(f"✅ Отправлено: {doctor['name']} -> {email_addr}")

            notes = "ПОВТОРНАЯ ОТПРАВКА" if FORCE_RESEND else ""
            sent_logs.append({
                "event": "SEND",
                "bot_id": bot_id,
                "target_date": target_date.strftime("%d.%m.%Y"),
                "doctor": doctor["name"],
                "email_address": email_addr or ",".join(RESERVE_EMAILS),
                "mapping": p["mapping"],
                "result": "SENT",
                "notes": notes,
            })

            status = make_status_value(email_addr or "резерв", FORCE_RESEND, False, is_reserve)
            for r in p["doctor_rows"]:
                n_updates.append({
                    "row": r["row_number"],
                    "col": COL_COMPLETION + 1,
                    "value": status,
                })

            successful_doctors += 1
            total_tasks += sum(len(r["tasks"]) for r in p["doctor_rows"])
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
            print(f"❌ Ошибка отправки {doctor['name']}: {exc}")

    if sent_logs or error_logs:
        print(f"BoTon_Log: пакетная запись SENT/ERROR ({len(sent_logs) + len(error_logs)})")
        append_log_entries(log_sheet, sent_logs + error_logs)

    if n_updates:
        print(f"Лист: пакетная запись N (отметки об отправке) ({len(n_updates)} строк)")
        batch_update_cells(tasks_sheet, n_updates, "запись отметок об отправке в N")

    print("\nИТОГ РАССЫЛКИ:")
    print(f"  Врачей обработано: {successful_doctors}")
    print(f"  Успешно отправлено: {successful_doctors - errors}")
    print(f"  Отправлено на резерв: {reserve_sent}")
    print(f"  Пропущено по журналу: {skipped_logged}")
    print(f"  Врач не найден: {not_found}")
    print(f"  Ошибок: {errors}")
    print(f"  Строк обновлено в N: {len(n_updates)}")
    print(f"  Всего заданий: {total_tasks}")

    return {
        "doctors": successful_doctors,
        "rows": len(n_updates),
        "tasks": total_tasks,
        "errors": errors,
        "skipped": skipped_logged,
        "reserve": reserve_sent,
        "not_found": not_found,
    }
