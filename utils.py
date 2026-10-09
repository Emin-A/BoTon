"""Общие функции нормализации и времени."""
import re
from datetime import datetime, time
from config import MOSCOW_TZ, ASSIGNMENT_HOUR_START, ASSIGNMENT_HOUR_END

def now_moscow():
    return datetime.now(MOSCOW_TZ)

def today_str():
    return now_moscow().strftime("%d.%m.%Y")

def is_assignment_time():
    t = now_moscow().time()
    return time(ASSIGNMENT_HOUR_START, 0) <= t <= time(ASSIGNMENT_HOUR_END, 0)

def normalize_text(value):
    if value is None:
        return ""
    value = str(value).replace("Ё", "Е").replace("ё", "е").replace("\xa0", " ")
    value = re.sub(r"[^\w\sА-Яа-я]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()

def normalize_email(value):
    return str(value or "").strip().lower()

def get_signature(name):
    """ФИО → 'фамилия инициалы' (устойчиво к перестановкам и точкам)."""
    if not name:
        return ""
    clean = str(name).lower().replace("ё", "е")
    clean = re.sub(r"[^a-zа-яё\s.]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    parts = [p for p in clean.split() if p]
    if not parts:
        return ""
    if len(parts) >= 2 and len(parts[0]) <= 2 and "." in parts[0]:
        surname = parts[-1].replace(".", "")
        initials = "".join(p.replace(".", "")[0] for p in parts[:-1] if p.replace(".", ""))
    else:
        surname = parts[0].replace(".", "")
        initials = "".join(p.replace(".", "")[0] for p in parts[1:] if p.replace(".", ""))
    return f"{surname} {initials}".strip()

def parse_date(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None