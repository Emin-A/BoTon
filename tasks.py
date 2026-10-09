"""Чтение, фильтрация и подготовка заданий."""
import re, hashlib
from collections import defaultdict
from datetime import datetime
from config import *
from utils import normalize_text, parse_date, get_signature
from google_sheets import sheets_call
def is_branch_match(value):
    v = normalize_text(value)
    if not v:
        return False
    return any(k in v for k in BRANCH_KEYWORDS)

def get_tasks_from_row(row, row_number):
    tasks = []
    for task_col, status_col, number in (
        (COL_TASK1, COL_STATUS1, 1),
        (COL_TASK2, COL_STATUS2, 2),
        (COL_TASK3, COL_STATUS3, 3),
    ):
        value = row[task_col] if len(row) > task_col else ""
        if not str(value).strip():
            continue
        tasks.append({
            "task_number": number,
            "task_col": task_col,
            "status_col": status_col,
            "row_number": row_number,
            "title": TASK_TITLES.get(number, f"Задание {number}"),
            "text": str(value).strip(),
        })
    return tasks

def load_task_rows(tasks_sheet, target_date):
    values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение заданий")
    rows = []
    stats = {
        "total": 0, "empty": 0, "bad_date": 0, "other_date": 0,
        "other_branch": 0, "already_processed": 0, "no_doctor": 0,
        "no_tasks": 0, "selected": 0,
    }

    for row_number, row in enumerate(values[1:], start=2):
        stats["total"] += 1
        if not row or not any(str(x).strip() for x in row):
            stats["empty"] += 1
            continue

        row = list(row)
        while len(row) <= COL_COMPLETION:
            row.append("")

        date_value = parse_date(row[COL_DATE])
        if date_value is None:
            stats["bad_date"] += 1
            continue
        if date_value != target_date:
            stats["other_date"] += 1
            continue
        if not is_branch_match(row[COL_BRANCH]):
            stats["other_branch"] += 1
            continue

        completion = str(row[COL_COMPLETION]).strip()
        if not FORCE_RESEND and not TEST_TO_RESERVE:
            if completion and completion.lower() not in ("null", "undefined"):
                stats["already_processed"] += 1
                continue

        doctor_name = str(row[COL_DOCTOR]).strip()
        if not doctor_name or doctor_name.lower() == "нет данных":
            stats["no_doctor"] += 1
            continue

        tasks = get_tasks_from_row(row, row_number)
        if not tasks:
            stats["no_tasks"] += 1
            continue

        rows.append({
            "row_number": row_number,
            "doctor_name": doctor_name,
            "patient_id": str(row[COL_PATIENT_ID]).strip(),
            "patient_name": str(row[COL_PATIENT_NAME]).strip(),
            "tasks": tasks,
            "row": row,
        })
        stats["selected"] += 1

    return rows, stats

def print_stats(stats):
    print("\n--- СТАТИСТИКА ПРОВЕРКИ СТРОК ---")
    print(f"Всего строк в таблице: {stats['total']}")
    print(f"Пустых строк: {stats['empty']}")
    print(f"Нераспознанная дата: {stats['bad_date']}")
    print(f"Другая дата (не сегодня): {stats['other_date']}")
    print(f"Другой филиал: {stats['other_branch']}")
    print(f"Уже обработано (столбец M): {stats['already_processed']}")
    print(f"Нет ФИО врача: {stats['no_doctor']}")
    print(f"Нет заданий: {stats['no_tasks']}")
    print(f"ИТОГО ПОДОШЛО ДЛЯ ОТПРАВКИ: {stats['selected']}")
    print("-----------------------------------\n")

def group_by_doctor(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[normalize_text(row["doctor_name"])].append(row)
    return grouped

def make_bot_id(target_date, doctor_signature, doctor_rows):
    parts = []
    for r in sorted(doctor_rows, key=lambda x: x["row_number"]):
        nums = ",".join(str(t["task_number"]) for t in r["tasks"])
        parts.append(f"{r['row_number']}:{nums}")
    signature = (
        f"{target_date.strftime('%Y%m%d')}|"
        f"{doctor_signature}|"
        f"{'/'.join(parts)}"
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10].upper()
    return f"BOTON-{target_date.strftime('%Y%m%d')}-{digest}"

def build_patients(doctor_rows):
    return [{
        "patient_name": r["patient_name"],
        "patient_id": r["patient_id"],
        "row_number": r["row_number"],
        "tasks": r["tasks"],
    } for r in doctor_rows]

def build_response_task_map(patients):
    mapping = defaultdict(list)
    for pnum, patient in enumerate(patients, start=1):
        tasks = patient["tasks"]
        for t_idx, task in enumerate(tasks):
            key = f"{pnum}" if len(tasks) == 1 else f"{pnum}{TASK_LETTERS[t_idx]}"
            mapping[key].append({
                "row_number": task["row_number"],
                "status_col": task["status_col"],
                "task_col": task["task_col"],
                "title": task["title"],
            })
    return dict(mapping)
