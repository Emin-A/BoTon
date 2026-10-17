"""Чтение, фильтрация и подготовка заданий. Колонки определяются по заголовкам."""
import hashlib
from collections import defaultdict
from config import *
from utils import normalize_text, parse_date
from google_sheets import sheets_call

# Резервные колонки, если заголовки не распознаны
DEFAULT_COLS = {
    "date": COL_DATE,
    "branch": COL_BRANCH,
    "doctor": COL_DOCTOR,
    "patient_id": COL_PATIENT_ID,
    "patient_name": COL_PATIENT_NAME,
    "source": COL_SOURCE,
    "task1": COL_TASK1,
    "task2": COL_TASK2,
    "task3": COL_TASK3,
    "comments": COL_COMMENTS,
}

# Ключевые слова для поиска колонок по заголовкам
COLUMN_KEYWORDS = {
    "date": ("дата",),
    "branch": ("филиал",),
    "doctor": ("врач", "фио врача"),
    "patient_id": ("идентификатор", "омс", "id"),
    "patient_name": ("фио", "пациент"),
    "task1": ("задание 1", "липиды"),
    "task2": ("задание 2", "выгрузка"),
    "task3": ("задание 3", "сд"),
    "source": ("источник",),
    "comments": ("комментар",),
}


def detect_columns(header):
    """Определяет индексы колонок по заголовкам. Каждая колонка используется один раз."""
    cols = dict(DEFAULT_COLS)
    used = set()
    
    # Порядок поиска: сначала специфичные, потом общие
    search_order = [
        "date", "branch", "doctor", "patient_id", "patient_name",
        "task1", "task2", "task3", "source", "comments"
    ]
    
    for key in search_order:
        keywords = COLUMN_KEYWORDS.get(key, ())
        for i, h in enumerate(header):
            if i in used:
                continue
            hn = normalize_text(h)
            if not hn:
                continue
            if any(kw in hn for kw in keywords):
                cols[key] = i
                used.add(i)
                break
    return cols


def is_branch_match(value):
    v = normalize_text(value)
    if not v:
        return False
    return any(k in v for k in BRANCH_KEYWORDS)


def get_tasks_from_row(row, row_number, cols):
    tasks = []
    for key, number in (("task1", 1), ("task2", 2), ("task3", 3)):
        col = cols[key]
        value = row[col] if len(row) > col else ""
        if not str(value).strip():
            continue
        tasks.append({
            "task_number": number,
            "task_col": col,
            "row_number": row_number,
            "title": TASK_TITLES.get(number, f"Задание {number}"),
            "text": str(value).strip(),
        })
    return tasks


def load_task_rows(tasks_sheet, target_date):
    """Читает лист, определяет колонки, фильтрует строки.
    Возвращает (rows, stats, cols)."""
    stats = {
        "total": 0, "empty": 0, "bad_date": 0, "other_date": 0,
        "other_branch": 0, "already_processed": 0,
        "no_doctor": 0, "no_tasks": 0, "selected": 0,
    }
    values = sheets_call(lambda: tasks_sheet.get_all_values(), "чтение заданий")
    if not values or len(values) < 2:
        return [], stats, dict(DEFAULT_COLS)

    cols = detect_columns(values[0])
    print("Определённые колонки: " +
          ", ".join(f"{k}={v + 1}" for k, v in cols.items()))

    max_col = max(cols.values())
    rows = []
    for row_number, row in enumerate(values[1:], start=2):
        stats["total"] += 1
        if not row or not any(str(x).strip() for x in row):
            stats["empty"] += 1
            continue
        row = list(row)
        while len(row) <= max_col:
            row.append("")

        date_value = parse_date(row[cols["date"]])
        if date_value is None:
            stats["bad_date"] += 1
            continue
        if date_value != target_date:
            stats["other_date"] += 1
            continue
        if not is_branch_match(row[cols["branch"]]):
            stats["other_branch"] += 1
            continue

        completion = str(row[cols["comments"]]).strip()
        if not FORCE_RESEND and not TEST_TO_RESERVE:
            if completion and completion.lower() not in ("null", "undefined"):
                stats["already_processed"] += 1
                continue

        doctor_name = str(row[cols["doctor"]]).strip()
        if not doctor_name or doctor_name.lower() == "нет данных":
            stats["no_doctor"] += 1
            continue

        tasks = get_tasks_from_row(row, row_number, cols)
        if not tasks:
            stats["no_tasks"] += 1
            continue

        rows.append({
            "row_number": row_number,
            "doctor_name": doctor_name,
            "patient_id": str(row[cols["patient_id"]]).strip(),
            "patient_name": str(row[cols["patient_name"]]).strip(),
            "branch": str(row[cols["branch"]]).strip(),
            "tasks": tasks,
            "row": row,
        })
        stats["selected"] += 1
    return rows, stats, cols


def print_stats(stats):
    print("\n--- СТАТИСТИКА ПРОВЕРКИ СТРОК ---")
    print(f"Всего строк в таблице: {stats['total']}")
    print(f"Пустых строк: {stats['empty']}")
    print(f"Нераспознанная дата: {stats['bad_date']}")
    print(f"Другая дата (не сегодня): {stats['other_date']}")
    print(f"Другой филиал: {stats['other_branch']}")
    print(f"Уже обработано (колонка комментариев): {stats['already_processed']}")
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
        nums = ", ".join(str(t["task_number"]) for t in r["tasks"])
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
                "title": task["title"],
            })
    return dict(mapping)
