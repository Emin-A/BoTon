"""Получение ответов врачей через IMAP и обновление H/J/L."""
import os, re, json, imaplib, email
from email.header import decode_header
from datetime import timedelta
from config import *
from utils import normalize_text, normalize_email, now_moscow
from log_store import load_log_records, is_message_processed, find_log_by_bot_id, append_log_entries
from google_sheets import sheets_call, batch_update_cells
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

def parse_status(text):
    n = normalize_text(text)
    if "не выполнено" in n:
        return "❌ Не выполнено"
    if "частично" in n:
        return "⚠️ Частично"
    if "выполнено" in n:
        return "✅ Выполнено"
    return None

def expand_status_items(text, mapping):
    result = []
    normalized = normalize_text(text)
    pattern = re.compile(
        r"(?:выполнено|частично|не\s+выполнено)\s*[:\-]?\s*([0-9]+[А-В]?)",
        flags=re.IGNORECASE,
    )
    for key in pattern.findall(normalized):
        key = key.upper()
        if key in mapping:
            result.append(key)
    return result

def collect_status_update(updates, current_values, row_number, status_col, new_status):
    sheet_col = status_col + 1
    current = ""
    if 1 <= row_number <= len(current_values):
        r = current_values[row_number - 1]
        if sheet_col - 1 < len(r):
            current = str(r[sheet_col - 1] or "").strip()

    if STATUS_PRIORITY.get(new_status, 0) < STATUS_PRIORITY.get(current, 0):
        return
    key = (row_number, sheet_col)
    if STATUS_PRIORITY.get(updates.get(key, ""), 0) >= STATUS_PRIORITY.get(new_status, 0):
        return
    updates[key] = new_status

def process_incoming_messages(tasks_sheet, log_sheet, doctors):
    print("\n----- ПРОВЕРКА ВХОДЯЩИХ ОТВЕТОВ -----")
    host = os.environ.get("IMAP_HOST", "")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ.get("IMAP_USER", "")
    password = os.environ.get("IMAP_PASSWORD", "")

    doctor_emails = [normalize_email(d["email"]) for d in doctors if d["email"]]
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

        candidates = []
        seen = set()
        for addr in doctor_emails:
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

        current_values = sheets_call(
            lambda: tasks_sheet.get_all_values(),
            "чтение Лист1 перед H/J/L",
        )

        status_updates = {}
        receive_logs = []

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

                doctor = next(
                    (d for d in doctors if normalize_email(d["email"]) == sender_email),
                    None,
                )
                if not doctor:
                    continue

                bot_id = extract_bot_id(subject, body)
                if not bot_id:
                    continue

                log_record = find_log_by_bot_id(records, bot_id)
                if not log_record:
                    continue

                try:
                    mapping = json.loads(log_record["mapping_json"] or "{}")
                except Exception:
                    mapping = {}
                if not mapping:
                    continue

                text_all = f"{subject}\n{body}"
                status_value = parse_status(text_all)
                if not status_value:
                    continue

                keys = expand_status_items(text_all, mapping)
                if not keys:
                    continue

                updated = []
                for key in keys:
                    for item in mapping.get(key, []):
                        collect_status_update(
                            status_updates, current_values,
                            item["row_number"], item["status_col"], status_value,
                        )
                        updated.append({
                            "key": key,
                            "row": item["row_number"],
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
                    "result": "PROCESSED",
                    "notes": json.dumps(updated, ensure_ascii=False),
                })
                processed += 1
                print(f"Ответ обработан: {doctor['name']} [{bot_id}]")

            except Exception as exc:
                print(f"IMAP: ошибка обработки сообщения: {exc}")

        if status_updates:
            updates = [
                {"row": rn, "col": sc, "value": val}
                for (rn, sc), val in status_updates.items()
            ]
            print(f"Лист1: пакетная запись H/J/L ({len(updates)} ячеек)")
            batch_update_cells(tasks_sheet, updates, "запись H/J/L")

        if receive_logs:
            print(f"BoTon_Log: пакетная запись RECEIVE ({len(receive_logs)})")
            append_log_entries(log_sheet, receive_logs)

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