"""SMTP и текст писем: лист заданий в виде таблицы + стихи (API с резервной подборкой)."""
import os
import ssl
import smtplib
import json
import unicodedata
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import *
from utils import now_moscow

# Маркер в начале строки задания (оставлен для совместимости)
TASK_MARKER = "🙂"
TASK_SUFFIX = "   Сделано 😃   /  нет, виноват 😢💩 "

# Строка подвала — что делать с листом
FOOTER_LINE_1 = "Лист распечатать, заполнить, передать на пост 2 этажа."
FOOTER_LINE_2 = "Царь молодец!"

# ============================================================
# ШИРИНЫ КОЛОНОК ТАБЛИЦЫ (в знакоместах). Меняй под свой принтер.
# Для А4 КНИЖНОЙ поставь: 20 / 22 / 22 / 20  (итого 97 знаков)
# Для А4 АЛЬБОМНОЙ можно:  26 / 28 / 22 / 30
# ============================================================
COL_PATIENT = 22
COL_TASK = 24
COL_STATUS = 22
COL_DETAILS = 26

# Что печатается в колонке «Статус» — врач ставит X в нужный квадратик
STATUS_LINES = [
    "[ ] Сделано 😃",
    "[ ] нет, виноват 😢💩",
]

# Настройки API стихов (по умолчанию Groq — бесплатный).
POEM_API_URL = os.environ.get("POEM_API_URL", "https://api.groq.com/openai/v1/chat/completions")
POEM_API_KEY = os.environ.get("POEM_API_KEY", "")
POEM_API_MODEL = os.environ.get("POEM_API_MODEL", "llama-3.3-70b-versatile")

POEM_PROMPT = (
    "Напиши ОДНО смешное четверостишие (ровно 4 строки) про царя-начальника "
    "и врачей-холопов в поликлинике: рассылка заданий по почте, КДП, липиды, "
    "отчёты в конце смены. Стиль лёгкий и добрый, без обид, с рифмой. "
    "Ответь только четырьмя строками стиха, без заголовков, кавычек и пояснений."
)

# Резервная подборка ГОТОВЫХ стихов (НЕ генератор).
CURATED_POEMS = [
    """Царь письмо отправил в семь часов утра,
Холопам на почту — работать пора.
Кто липиды правит, кто КДП ведёт,
К вечеру отметку царю несёт.""",
    """В отделенье тихо, лишь клавиш стук:
Царь разослал порученья для слуг.
Квадратик отмечен — и на сердце покойно:
Царь молодец, и холопы довольны.""",
    """Царь не спит на печи, царь в таблицы глядит:
Кто не сдал КДП — того ждёт визит.
Но не гневайся, царь, твои холопы верны —
К вечеру все отметки в таблице полны.""",
    """Шлёт царь-батюшка письма с утренней зорькой,
Холопы отвечают исправно, хоть горько.
Зато к вечеру, царь, всё исполнено в срок —
Получай свой отчёт, дорогой наш царёк!""",
    """Вздыхает холоп: «Снова письма с утра!»
Но спорить не смеет: работать пора.
Ведь царь-то печётся не ради забав —
Чтоб каждый пациент был здоров и прав.""",
    """Царь и холоп — одна семья,
Без них не крутится земля:
Царь шлёт задания с утра,
Холоп их делает. Ура!""",
    """Коль царь прислал письмо — не унывай,
Читай, отмечай и выполняй.
А царь, получив твой отчёт-доклад,
Скажет: «Холоп мой, ты просто клад!»""",
    """Лист на А4 — как царский указ:
Всё по пунктам, без лишних фраз.
Холоп отчитался — и царь успокоен:
Держава в порядке, и каждый доволен.""",
]

_poem_index = 0


def generate_poem_via_api():
    """Пробует сгенерировать стих через API. Возвращает None, если ключа нет."""
    if not POEM_API_KEY.strip():
        return None
    body = json.dumps({
        "model": POEM_API_MODEL,
        "messages": [{"role": "user", "content": POEM_PROMPT}],
        "temperature": 1.1,
        "max_tokens": 120,
    }).encode("utf-8")
    req = urllib.request.Request(
        POEM_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {POEM_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 4:
        return "\n".join(lines[:4])
    if lines:
        return "\n".join(lines)
    return None


def pick_curated_poem(target_date):
    """Ротация готовых стихов: сдвиг по дате + счётчик писем внутри запуска."""
    global _poem_index
    idx = (target_date.toordinal() + _poem_index) % len(CURATED_POEMS)
    _poem_index += 1
    return CURATED_POEMS[idx]


def generate_poem(target_date):
    """Стих: API → при любой осечке резервная подборка готовых стихов."""
    if POEM_API_KEY.strip():
        try:
            poem = generate_poem_via_api()
            if poem:
                print("🎭 Стих: сгенерирован через API")
                return poem
            print("⚠️ API вернул пустой ответ → резервная подборка")
        except Exception as exc:
            print(f"⚠️ API стихов недоступен ({exc}) → резервная подборка")
    else:
        print("🎭 API-ключ POEM_API_KEY не задан → резервная подборка готовых стихов")
    return pick_curated_poem(target_date)


def _short_text(task):
    """Убирает повторы: 'провести КДП с диагнозом: X' → 'диагноз X'."""
    text = (task.get("text") or "").strip()
    low = text.lower()
    if low.startswith("провести кдп с диагнозом:"):
        return ("диагноз " + text[len("провести кдп с диагнозом:"):]).strip()
    if low in ("провести кдп", "провести кдп."):
        return ""
    return text


# ============================================================
# ОТРИСОВКА ТАБЛИЦЫ (с учётом двойной ширины смайликов)
# ============================================================
def _char_width(ch):
    """Сколько знакомест занимает символ: смайлики = 2, обычные буквы = 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _disp_width(text):
    """Реальная ширина строки на экране/при печати моноширинным шрифтом."""
    return sum(_char_width(ch) for ch in str(text))


def _pad(text, width):
    """Дополняет строку пробелами до нужной ШИРИНЫ, а не до числа символов."""
    return str(text) + " " * max(0, width - _disp_width(text))


def _wrap_cell(text, width):
    """Переносит текст внутри ячейки по реальной ширине колонки."""
    text = str(text or "").strip()
    if not text:
        return [""]
    lines = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if _disp_width(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            # слово длиннее колонки — режем по символам
            while _disp_width(word) > width:
                chunk = ""
                for ch in word:
                    if _disp_width(chunk + ch) > width:
                        break
                    chunk += ch
                if not chunk:
                    chunk = word[0]
                lines.append(chunk)
                word = word[len(chunk):]
            current = word
        if current:
            lines.append(current)
    return lines or [""]


def _border(widths, left, mid, right):
    """Горизонтальная линия таблицы."""
    return left + mid.join("─" * (w + 2) for w in widths) + right


def _table_row(cells, widths):
    """Одна строка таблицы (может занимать несколько экранных строк)."""
    wrapped = [_wrap_cell(c, w) for c, w in zip(cells, widths)]
    height = max(len(col) for col in wrapped)
    for col in wrapped:
        while len(col) < height:
            col.append("")
    out = []
    for i in range(height):
        parts = [_pad(wrapped[j][i], widths[j]) for j in range(len(widths))]
        out.append("│ " + " │ ".join(parts) + " │")
    return out


def build_email_body(doctor, patients, bot_id, target_date, branch=""):
    """Лист заданий в виде таблицы + стих в конце.
    Поддерживает ЛЮБОЕ количество заданий у пациента (буквы А..К)."""
    widths = [COL_PATIENT, COL_TASK, COL_STATUS, COL_DETAILS]
    table_width = sum(w + 2 for w in widths) + len(widths) + 1
    wide = "=" * table_width
    thin = "-" * table_width

    total_tasks = sum(len(p["tasks"]) for p in patients)

    lines = [
        wide,
        f"  ЛИСТ ЗАДАНИЙ НА {target_date.strftime('%d.%m.%Y')}",
    ]
    if branch:
        lines.append(f"  Филиал: {branch}")
    lines += [
        wide,
        f"Врач: {doctor['name']}",
        f"Пациентов: {len(patients)} | Заданий: {total_tasks}",
        thin,
        "",
        "Отметьте X в нужном квадратике напротив каждого задания.",
        "Что не отмечено — считается «Не выполнено».",
        "",
        _border(widths, "┌", "┬", "┐"),
    ]
    lines += _table_row(["Пациент", "Задание", "Статус", "Расшифровка задания"], widths)
    lines.append(_border(widths, "├", "┼", "┤"))

    first_row = True
    for patient in patients:
        p_name = patient.get("patient_name") or "ФИО не указано"
        p_id = patient.get("patient_id") or "не указан"
        patient_cell = f"{p_name}\nОМС {p_id}"

        for task in patient.get("tasks", []):
            if not first_row:
                lines.append(_border(widths, "├", "┼", "┤"))
            first_row = False

            letter = str(task.get("letter", "")).strip()
            short = str(task.get("short", "") or "").strip().lstrip("—-–: ").strip()
            label = f"{letter} — {short}" if short else letter
            details = _short_text(task)
            status = "\n".join(STATUS_LINES)

            lines += _table_row([patient_cell, label, status, details], widths)

    lines.append(_border(widths, "└", "┴", "┘"))

    poem = generate_poem(target_date)
    lines += [
        thin,
        FOOTER_LINE_1,
        f"Лист № {bot_id} | {now_moscow().strftime('%d.%m.%Y %H:%M')}.",
        "",
    ]
    lines += ["   " + ln for ln in poem.splitlines()]
    lines += [
        "",
        FOOTER_LINE_2,
        wide,
    ]
    return "\n".join(lines)


def send_email(to_email, subject, body):
    """Отправка plain-text письма."""
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)

    if not to_email:
        raise RuntimeError("Пустой получатель")
    if not host or not user or not password:
        raise RuntimeError("Не настроены SMTP_HOST/SMTP_USER/SMTP_PASSWORD")

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
