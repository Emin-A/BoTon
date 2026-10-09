"""Загрузка и сопоставление врачей."""
from config import *
from utils import get_signature
def load_doctors_map(doctors_sheet):
    values = doctors_sheet.get_all_values()
    result = []
    for row in values[1:]:
        if not row:
            continue
        name = str(row[0]).strip() if len(row) > 0 else ""
        chat_id = str(row[1]).strip() if len(row) > 1 else ""
        email_addr = str(row[2]).strip() if len(row) > 2 else ""
        if not name:
            continue
        result.append({"name": name, "chat_id": chat_id, "email": email_addr})
    print(f"Врачей в базе: {len(result)}")
    return result

def find_doctor_robust(task_name, doctors):
    sig = get_signature(task_name)
    if not sig:
        return None

    # 1) точное совпадение подписи
    for d in doctors:
        if get_signature(d["name"]) == sig:
            return d

    # 2) совпадение по фамилии + инициалам (частичное)
    sig_parts = sig.split()
    surname = sig_parts[0] if sig_parts else ""
    initials = sig_parts[1] if len(sig_parts) > 1 else ""
    for d in doctors:
        db_sig = get_signature(d["name"])
        db_parts = db_sig.split()
        db_sur = db_parts[0] if db_parts else ""
        db_ini = db_parts[1] if len(db_parts) > 1 else ""
        if db_sur == surname and surname:
            if not initials or not db_ini \
               or initials.startswith(db_ini) or db_ini.startswith(initials):
                return d

    # 3) подстрочное вхождение фамилии (>=4 символов)
    for d in doctors:
        db_sig = get_signature(d["name"])
        db_sur = db_sig.split()[0] if db_sig else ""
        if len(surname) >= 4 and len(db_sur) >= 4 \
           and (db_sur in surname or surname in db_sur):
            return d
    return None