"""Работа с базой врачей из Google Sheets."""
import re
from config import *
from utils import normalize_text, normalize_email
from google_sheets import sheets_call

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Резервные индексы столбцов (если заголовки не распознаны): A=ФИО, C=Email
FALLBACK_NAME_COL = 0
FALLBACK_EMAIL_COL = 2


def is_valid_email(value):
    """Проверяет, что значение похоже на email (есть @ и точка после неё)."""
    return bool(EMAIL_RE.match(str(value or "").strip()))


def _find_col(header, *keywords):
    """Ищет индекс столбца по ключевым словам в заголовке."""
    for i, h in enumerate(header):
        for kw in keywords:
            if kw in h:
                return i
    return None


def load_doctors(doctors_sheet):
    """Читает таблицу врачей, определяя столбцы по заголовкам."""
    values = sheets_call(lambda: doctors_sheet.get_all_values(), "чтение базы врачей")
    if not values or len(values) < 2:
        print("⚠️  Таблица врачей пуста или не имеет заголовков")
        return []

    header = [normalize_text(h) for h in values[0]]
    name_col = _find_col(header, "фио", "врач", "фамилия")
    email_col = _find_col(header, "email", "почта", "e-mail", "mail")
    branch_col = _find_col(header, "филиал", "подразделение")

    if name_col is None:
        name_col = FALLBACK_NAME_COL
    if email_col is None:
        email_col = FALLBACK_EMAIL_COL

    msg = f"Столбцы врачей: ФИО={name_col + 1}, Email={email_col + 1}"
    if branch_col is not None:
        msg += f", Филиал={branch_col + 1}"
    print(msg)

    doctors = []
    for row in values[1:]:
        if not row or not str(row[name_col]).strip():
            continue
        raw_email = row[email_col] if len(row) > email_col else ""
        email = normalize_email(raw_email)
        if email and not is_valid_email(email):
            print(f"⚠️ Некорректный email у врача '{str(row[name_col]).strip()}': "
                  f"'{email}' — будет использована резервная почта")
            email = ""
        doctors.append({
            "name": str(row[name_col]).strip(),
            "email": email,
            "branch": str(row[branch_col]).strip() if branch_col is not None and len(row) > branch_col else "",
        })

    print(f"Загружено врачей из таблицы: {len(doctors)}")
    return doctors


def find_doctor_robust(source_name, doctors):
    """Ищет врача по ФИО с нечётким сравнением."""
    if not source_name or not doctors:
        return None

    source_norm = normalize_text(source_name)

    # Точное совпадение после нормализации
    for doc in doctors:
        if normalize_text(doc["name"]) == source_norm:
            return doc

    # Совпадение по фамилии и инициалам
    source_parts = source_norm.split()
    for doc in doctors:
        doc_norm = normalize_text(doc["name"])
        doc_parts = doc_norm.split()
        if source_parts and doc_parts and source_parts[0] == doc_parts[0]:
            if len(source_parts) == 1 or len(doc_parts) == 1:
                return doc
            source_initials = set(p[0] for p in source_parts[1:] if p)
            doc_initials = set(p[0] for p in doc_parts[1:] if p)
            if source_initials & doc_initials:
                return doc

    return None
