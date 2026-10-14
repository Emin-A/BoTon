"""Получение ответов врачей через IMAP и обновление H/J/L."""
import os, re, json, imaplib, email
from email.header import decode_header
from datetime import timedelta
from config import *
from utils import normalize_text, normalize_email, now_moscow
from log_store import load_log_records, is_message_processed, find_log_by_bot_id, append_log_entries
from google_sheets import batch_update_cells
from mailer import send_email

CHECK_MARKS = set("xXхХvV✓✔+*")

CHECKBOX_RE = re.compile(
    r"^[\[(]\s*([^\]\)\s]?)\s*[\])]\s*(\d{1,3})([АБВABabабв])?(?![0-9])"
)
LATIN_TO_CYR = {"A": "А", "B": "В"}


def decode_mime_header(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def extract_message_body(msg):
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ctype == "text/plain" and "attachment" not in disp.lower():
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(cs, errors="replace"))
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    cs = msg.get_content_charset() or "utf-8"
    return payload.decode(cs, errors="replace")


def extract_bot_id(subject, body):
    text = f"{subject}\n{body}"
    m = re.search(r"BOTON-ID\s*[:\-]?\s*(BOTON-[A-Za-z0-9_.\-]+)", text, flags=re.IGNORECASE)
    return m.group(1) if m else ""


def resolve_keys(digits, letter, mapping):
    key = f"{digits}{letter}"
    if key in mapping:
        return [key]
    if not letter:
        return [k for k in mapping if re.fullmatch(rf"{digits}[АБВ]", k)]
    return []


def parse_reply(body, mapping):
    done, failed = set(), set()
    seen = False
    for raw_line in body.splitlines():
        line = re.sub(r"^[\s>]+", "", raw_line)
        m = CHECKBOX_RE.match(line)
        if not m:
            continue
        digits = str(int(m.group(2)))
        letter = (m.group(3) or "").upper()
        letter = LATIN_TO_CYR.get(letter, letter)
        keys = resolve_keys(digits, letter, mapping)
        if not keys:
            continue
        norm = normalize_text(line[m.end():])
        if "не состоялась" in norm or "не состоялось" in norm:
            target = failed
        elif "отработан" in norm:
            target = done
        else:
            continue
        seen = True
        if m.group(1) in CHECK_MARKS:
            target.update(keys)

    if not seen:
        return None

    result = {}
    for key in mapping:
        if key in failed:
            result[key] = STATUS_CONSULT_FAILED
        elif key in done:
            result[key] = STATUS_DONE
        else:
            result[key] = STATUS_NOT_DONE
    return result


def _col_letter(index):
    return chr(ord("A") + index) if 0 <= index < 26 else str(index + 1)


def send_test_notice(bot_id, updated):
    subject = f"[ТЕСТ BoTon] Результат разбора вашего ответа [{bot_id}]"
    if updated is None:
        body = (
            "ТЕСТ BoTon: ваше письмо получено, но отметки не найдены.\n\n"
            "Проверьте, что вы нажали «Ответить», текст письма с квадратиками "
            "остался, а пустые квадратики [ ] заменены на [x].\n"
            f"BOTON-ID: {bot_id}\n"
        )
    else:
        lines = [
            "ТЕСТ BoTon: ваш ответ получен и разобран.",
            "",
            "Так программа поняла отметки:",
        ]
        for u in updated:
            lines.append(
                f"  {u['key']} → строка {u['row']}, столбец {u['column']} "
                f"({u['title']}): {u['status']}"
            )
        lines.append("")
        lines.append("Что не отмечено, записано как «Задание не отработано».")
        if TEST_WRITE_TABLE:
            lines.append("Статусы ЗАПИСАНЫ в таблицу (столбцы H/J/L): "
                         "после проверки очистите эти ячейки вручную.")
        else:
            lines.append("В таблицу в тестовом режиме ничего не записано.")
        lines.append(f"BOTON-ID: {bot_id}")
        body = "\n".join(lines)

    try:
        send_email(TEST_EMAIL, subject, body)
        print(f"Тест: результат разбора отправлен на {TEST_EMAIL}")
    except Exception as exc:
        print(f"Тест: не удалось отправить результат разбора: {exc}")


def process_incoming_messages(tasks_sheet, log_sheet, doctors):
    print("\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----")
    host = os.environ.get("IMAP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER", "")
    password = os.environ.get("IMAP_PASSWORD", "")

    test_email = normalize_email(TEST_EMAIL)
    report_email = normalize_email(REPORT_EMAIL)
    # Нормализуем все резервные адреса
    reserve_emails = [normalize_email(a) for a in RESERVE_EMAILS if a]
    doctor_emails = [normalize_email(d["email"]) for d in doctors if d["email"]]

    # Ищем письма от: врачей + TEST_EMAIL + ВСЕХ резервных адресов
    extra = [a for a in [test_email, report_email] + reserve_emails if a]
    search_addrs = list(dict.fromkeys(doctor_emails + extra))

    processed = 0
    mail = None
    try:
        print(f"IMAP: подключение к {host}:{port}...")
        mail = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT)
        mail.login(user, password)
        status, _ = mail.select("INBOX", readonly=True)
        if status != "OK":
            print("IMAP: не удалось открыть INBOX")
            return 0

        records = load_log_records(log_sheet)
        since_date = (now_moscow().date() - timedelta(days=IMAP_LOOKBACK_DAYS))
        since_imap = since_date.strftime("%d-%b-%Y")
        print(f"IMAP: ищем письма с {since_date.strftime('%d.%m.%Y')}")
        print(f"IMAP: адреса поиска: {search_addrs}")

        candidates = []
        seen = set()
        for addr in search_addrs:
            if not addr:
                continue
            try:
                st, data = mail.search(None, "FROM", addr, "SINCE", since_imap)
                if st != "OK" or not data or not data[0]:
                    continue
                nums = data[0].split()[-IMAP_MAX_PER_DOCTOR:]
                for n in nums:
                    key = n.decode(errors="ignore")
                    if key not in seen:
                        seen.add(key)
                        candidates.append((n, addr))
            except Exception as exc:
                print(f"IMAP: ошибка поиска {addr}: {exc}")

        candidates = candidates[-IMAP_MAX_TOTAL:]
        print(f"IMAP: найдено потенциальных писем: {len(candidates)}")

        status_updates = {}
        receive_logs = []
        test_notices = []

        for num, expected_email in candidates:
            try:
                st, data = mail.fetch(num, "(BODY.PEEK[])")
                if st != "OK" or not data:
                    continue
                raw = None
                for item in data:
                    if isinstance(item, tuple):
                        raw = item[1]
                        break
                if not raw:
                    continue

                msg = email.message_from_bytes(raw)
                message_id = (msg.get("Message-ID", "") or "").strip()
                if is_message_processed(records, message_id):
                    continue

                from_header = decode_mime_header(msg.get("From", ""))
                subject = decode_mime_header(msg.get("Subject", ""))
                body = extract_message_body(msg)

                m = re.search(r"<([^>]+)>", from_header)
                sender_email = normalize_email(m.group(1) if m else from_header)

                if sender_email != expected_email:
                    continue

                bot_id = extract_bot_id(subject, body)
                if not bot_id:
                    continue

                is_test = bot_id.upper().startswith("BOTON-TEST")
                doctor = next(
                    (d for d in doctors if normalize_email(d["email"]) == sender_email),
                    None,
                )

                log_record = find_log_by_bot_id(records, bot_id)
                if not log_record:
                    continue

                if is_test:
                    # Тестовые ответы принимаются только с адреса TEST_EMAIL.
                    if sender_email != test_email:
                        continue
                    if not doctor:
                        doctor = {"name": "ТЕСТ", "email": sender_email}
                else:
                    # Принимаем ответ, если отправитель совпадает с адресом,
                    # на который ушло письмо по журналу (в т.ч. резервному).
                    log_email = normalize_email(
                        log_record.get("email_address") or log_record.get("email") or ""
                    )
                    # Также принимаем, если отправитель — любой из резервных адресов
                    if not doctor and sender_email != log_email and sender_email not in reserve_emails:
                        continue
                    if not doctor:
                        doctor = {"name": log_record.get("doctor", "Неизвестен"), "email": sender_email}

                try:
                    mapping = json.loads(log_record["mapping_json"] or "{}")
                except Exception:
                    mapping = {}

                if not mapping:
                    continue

                answers = parse_reply(body, mapping)
                if not answers:
                    if is_test:
                        test_notices.append((bot_id, None))
                    receive_logs.append({
                        "event": "RECEIVE",
                        "bot_id": bot_id,
                        "target_date": log_record["date"],
                        "doctor": doctor["name"],
                        "email_address": sender_email,
                        "mapping": mapping,
                        "message_id": message_id,
                        "result": "TEST_UNRECOGNIZED" if is_test else "UNRECOGNIZED",
                        "notes": "отметки не найдены",
                    })
                    continue

                updated = []
                for key, status_value in answers.items():
                    for item in mapping.get(key, []):
                        if not is_test or TEST_WRITE_TABLE:
                            status_updates[(item["row_number"], item["status_col"] + 1)] = status_value
                        updated.append({
                            "key": key,
                            "row": item["row_number"],
                            "column": _col_letter(item["status_col"]),
                            "title": item["title"],
                            "status": status_value,
                        })

                receive_logs.append({
                    "event": "RECEIVE",
                    "bot_id": bot_id,
                    "target_date": log_record["date"],
                    "doctor": doctor["name"],
                    "email_address": sender_email,
                    "mapping": mapping,
                    "message_id": message_id,
                    "result": "TEST_PROCESSED" if is_test else "PROCESSED",
                    "notes": json.dumps(updated, ensure_ascii=False),
                })

                if is_test:
                    test_notices.append((bot_id, updated))
                    print(f"Тестовый ответ обработан [{bot_id}]")
                else:
                    processed += 1
                    print(f"Ответ обработан: {doctor['name']} [{bot_id}]")

            except Exception as exc:
                print(f"IMAP: ошибка обработки сообщения: {exc}")

        if status_updates:
            updates = [
                {"row": rn, "col": sc, "value": val}
                for (rn, sc), val in status_updates.items()
            ]
            print(f"Лист: пакетная запись H/J/L ({len(updates)} ячеек)")
            batch_update_cells(tasks_sheet, updates, "запись H/J/L")

        if receive_logs:
            print(f"BoTon_Log: пакетная запись RECEIVE ({len(receive_logs)})")
            append_log_entries(log_sheet, receive_logs)

        for bot_id, updated in test_notices:
            send_test_notice(bot_id, updated)

        return processed

    except Exception as exc:
        print(f"IMAP ERROR: {exc}")
        return processed
    finally:
        if mail is not None:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


if __name__ == "__main__":
    import gspread
    from google.oauth2.service_account import Credentials
    from doctors import load_doctors

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    tasks_spreadsheet = gc.open_by_key(TASKS_SPREADSHEET_ID)
    target_date = now_moscow().date()
    sheet_name = target_date.strftime(TASKS_SHEET_DATE_FORMAT)

    try:
        tasks_sheet = tasks_spreadsheet.worksheet(sheet_name)
        print(f"✅ Открыта вкладка: {sheet_name}")
    except gspread.WorksheetNotFound:
        print(f"⚠️ Вкладка '{sheet_name}' не найдена. Используем fallback: '{TASKS_SHEET_NAME_FALLBACK}'")
        tasks_sheet = tasks_spreadsheet.worksheet(TASKS_SHEET_NAME_FALLBACK)

    log_spreadsheet = gc.open_by_key(LOG_SPREADSHEET_ID)
    log_sheet = log_spreadsheet.worksheet(LOG_SHEET_NAME)
    print(f"📋 Журнал открыт: {LOG_SPREADSHEET_ID} / {LOG_SHEET_NAME}")

    doctors_sheet = gc.open_by_key(DOCTORS_SPREADSHEET_ID).worksheet(DOCTORS_SHEET_NAME)
    doctors = load_doctors(doctors_sheet)

    n = process_incoming_messages(tasks_sheet, log_sheet, doctors)
    print(f"\nОбработано ответов: {n}")
