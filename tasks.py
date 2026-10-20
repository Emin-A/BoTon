"""Чтение, фильтрация и подготовка заданий.
Колонки и КОЛИЧЕСТВО заданий определяются автоматически по заголовкам."""
import re
import hashlib
from collections import defaultdict
from config import *
from utils import normalize_text, parse_date
from google_sheets import sheets_call

# Базовые колонки (если заголовки не распознаны)
DEFAULT_COLS = {
    "date": COL_DATE,
    "branch": COL_BRANCH,
    "doctor": COL_DOCTOR,
    "patient_id": COL_PATIENT_ID,
    "patient_name": COL_PATIENT_NAME,
    "source": COL_SOURCE,
    "comments": COL_COMMENTS,
}

COLUMN_KEYWORDS = {
    "date": ("дата",),
    "branch": ("филиал",),
    "doctor": ("врач",),
    "patient_id": ("идентификатор", "омс"),
    "patient_name": ("фио",),
    "source": ("источник",),
    "comments": ("комментар",),
}

# Колонки заданий по умолчанию (если в шапке нет слова «задание»)
FALLBACK_TASKS = [
    {"col": COL_TASK1, "title": TASK_TITLES.get(1, "Задание 1"), "short": "липиды"},
    {"col": COL_TASK2, "title": TASK_TITLES.get(2, "Задание 2"), "short": "КДП выгрузка"},
    {"col": COL_TASK3, "title": TASK_TITLES.get(3, "Задание 3"), "short": "КДП СД"},
]

# Слова-исключения: такие колонки НЕ считаются заданиями
TASK_EXCLUDE = ("отметка", "выполнени", "статус")


def _short_from_header(header_raw):
    """Короткое имя задания: текст в скобках заголовка или сам заголовок без 'Задание N'."""
    h = str(header_raw or "").strip()
    m = re.search(r"\(([^)]+)\)", h)
    if m:
        return m.group(1).strip()
    cleaned = re.sub(r"^задание\s*\d*\s*", "", normalize_text(h)).strip()
    return cleaned or "задание"


def detect_columns(header):
    """Определяет базовые колонки и ВСЕ колонки заданий по заголовкам."""
    cols = dict(DEFAULT_COLS)
    used = set()
    for key, keywords in COLUMN_KEYWORDS.items():
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

    task_cols = []
    for i, h in enumerate(header):
        hn = normalize_text(h)
        if not hn or "задание" not in hn:
            continue
        if any(x in hn for x in TASK_EXCLUDE):
            continue
        m = re.search(r"задание\s*(\d+)", hn)
        num = int(m.group(1)) if m else len(task_cols) + 1
        task_cols.append({
            "col": i,
            "number": num,
            "title": str(h).strip(),
            "short": _short_from_header(h),
        })

    if task_cols:
        task_cols.sort(key=lambda t: (t["number"], t["col"]))
        cols["tasks"] = task_cols
    else:
        cols["tasks"] = [dict(t) for t in FALLBACK_TASKS]
    return cols


def is_branch_match(value):
    v = normalize_text(value)
    if not v:
        return False
    return any(k in v for k in BRANCH_KEYWORDS)


def get_tasks_from_row(row, row_number, cols):
    """Собирает непустые задания строки, присваивая буквы А, Б, В..."""
    present = []
    for tc in cols["tasks"]:
        value = row[tc["col"]] if len(row) > tc["col"] else ""
        if str(value).strip():
            present.append((tc, str(value).strip()))

    multi = len(present) > 1
    tasks = []
    for pos, (tc, text) in enumerate(present):
        letter_raw = TASK_LETTERS[pos] if pos < len(TASK_LETTERS) else str(pos + 1)
        tasks.append({
            "task_number": pos + 1,
            "task_col": tc["col"],
            "row_number": row_number,
            "title": tc["title"],
            "short": tc["short"],
            "letter": f"{letter_raw}) " if multi else "",
            "key_letter": letter_raw if multi else "",
            "text": text,
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
        return [], stats, dict(DEFAULT_COLS, tasks=[dict(t) for t in FALLBACK_TASKS])

    cols = detect_columns(values[0])
    base_info = ", ".join(f"{k}={v + 1}" for k, v in cols.items() if k != "tasks")
    print(f"Определённые колонки: {base_info}, заданий={len(cols['tasks'])}")
    for tc in cols["tasks"]:
        print(f"   Задание {tc['number']}: колонка {tc['col'] + 1} «{tc['title']}» → в письме «{tc['short']}»")

    max_col = max([cols[k] for k in DEFAULT_COLS] + [t["col"] for t in cols["tasks"]])
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
        for task in patient["tasks"]:
            key = f"{pnum}{task['key_letter']}"
            mapping[key].append({
                "row_number": task["row_number"],
                "title": task["title"],
            })
    return dict(mapping)
