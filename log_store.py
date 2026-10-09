"""Хранилище событий BoTon_Log."""
import json
import gspread
from config import *
from google_sheets import sheets_call
from utils import now_moscow
def ensure_log_sheet(book):
    try:
        ws = book.worksheet(LOG_SHEET_NAME)
        print(f"BoTon_Log: {LOG_SHEET_NAME}")
        return ws
    except gspread.WorksheetNotFound:
        print(f"Google Sheets: создаю лист {LOG_SHEET_NAME}")
        ws = sheets_call(
            lambda: book.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_HEADERS)),
            "создание BoTon_Log",
        )
        sheets_call(
            lambda: ws.append_row(LOG_HEADERS, value_input_option="USER_ENTERED"),
            "заголовки BoTon_Log",
        )
        return ws

def make_log_row(event, bot_id="", target_date="", doctor="", email_address="",
                 mapping=None, message_id="", result="", notes=""):
    return [
        now_moscow().strftime("%d.%m.%Y %H:%M:%S"),
        event, bot_id, target_date, doctor, email_address,
        json.dumps(mapping or {}, ensure_ascii=False),
        message_id, result, notes,
    ]

def append_log_entries(log_sheet, entries):
    if not entries:
        return
    rows = []
    for e in entries:
        if isinstance(e, dict):
            rows.append(make_log_row(
                event=e.get("event", ""),
                bot_id=e.get("bot_id", ""),
                target_date=e.get("target_date", ""),
                doctor=e.get("doctor", ""),
                email_address=e.get("email_address", ""),
                mapping=e.get("mapping", {}),
                message_id=e.get("message_id", ""),
                result=e.get("result", ""),
                notes=e.get("notes", ""),
            ))
        else:
            rows.append(e)
    sheets_call(
        lambda: log_sheet.append_rows(rows, value_input_option="USER_ENTERED"),
        "пакетная запись BoTon_Log",
    )

def load_log_records(log_sheet):
    values = log_sheet.get_all_values()
    records = []
    for row in values[1:]:
        while len(row) < 10:
            row.append("")
        records.append({
            "timestamp": row[0], "event": row[1], "bot_id": row[2],
            "date": row[3], "doctor": row[4], "email": row[5],
            "mapping_json": row[6], "message_id": row[7],
            "result": row[8], "notes": row[9],
        })
    return records

def get_send_state(records, bot_id):
    for r in reversed(records):
        if r["event"] == "SEND" and r["bot_id"] == bot_id and r["result"] in ("SENT", "PENDING"):
            return r["result"]
    return ""

def find_log_by_bot_id(records, bot_id):
    for r in reversed(records):
        if r["event"] == "SEND" and r["bot_id"] == bot_id and r["result"] == "SENT":
            return r
    return None

def is_message_processed(records, message_id):
    if not message_id:
        return False
    return any(r["event"] == "RECEIVE" and r["message_id"] == message_id for r in records)