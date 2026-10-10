"""Работа с базой врачей из Google Sheets."""
from config import *
from utils import normalize_text, normalize_email
from google_sheets import sheets_call


def load_doctors(doctors_sheet):
    """Читает таблицу врачей и возвращает список словарей."""
    values = sheets_call(lambda: doctors_sheet.get_all_values(), "чтение базы врачей")
    if not values or len(values) < 2:
        print("⚠️  Таблица врачей пуста или не имеет заголовков")
        return []
    
    doctors = []
    for row in values[1:]:  # пропускаем заголовок
        if not row or not str(row[0]).strip():
            continue
        doctor = {
            "name": str(row[0]).strip(),
            "email": normalize_email(row[1]) if len(row) > 1 else "",
            "branch": str(row[2]).strip() if len(row) > 2 else "",
        }
        doctors.append(doctor)
    
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
    
    # Совпадение по подстроке (фамилия + инициалы)
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
