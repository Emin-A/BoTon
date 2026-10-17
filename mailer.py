"""SMTP и текст писем (компактная печатная форма под A4 + ротация шуток)."""
import os
import ssl
import smtplib
import itertools
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import *
from utils import now_moscow

# Короткие названия заданий для компактного листа
TASK_SHORT = {
    1: "липиды",
    2: "КДП выгрузка",
    3: "КДП СД",
}

# Маркер в начале строки задания
TASK_MARKER = "🙂"

# Хвостик после каждой строки задания
TASK_SUFFIX = "   Выполнено: ДА    /  нет, виноват :(. "

# Шуточные строки подвала
FOOTER_LINE_1 = "Лист сохраните, вдруг когда-нибудь понадобится, покажете внукам."
FOOTER_LINE_2 = "Царь молодец!"

# Четверостишия про царя и холопов: ротация на каждое письмо, сдвиг по дате.
# Хотите своё — допишите блок из четырёх строк в конец списка.
POEMS = [
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

# Счётчик писем внутри одного запуска (для ротации стихов)
_poem_counter = itertools.count()


def pick_poem(target_date):
    """Выбирает следующее четверостишие: сдвиг по дате + счётчик писем."""
    idx = (target_date.toordinal() + next(_poem_counter)) % len(POEMS)
    return POEMS[idx]


def _short_text(task):
    """Убирает повторы: 'провести КДП с диагнозом: X' → 'диагноз X'."""
    text = (task.get("text") or "").strip()
    low = text.lower()
    if low.startswith("провести кдп с диагнозом:"):
        return ("диагноз " + text[len("провести кдп с диагнозом:"):]).strip()
    if low in ("провести кдп", "провести кдп."):
        return ""
    return text


def build_email_body(doctor, patients, bot_id, target_date, branch=""):
    """Компактный лист заданий под A4 + свежее четверостишие в конце."""
    wide = "=" * 44
    thin = "-" * 44
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
    ]

    for pnum, patient in enumerate(patients, start=1):
        p_name = patient["patient_name"] or "ФИО не указано"
        p_id = patient["patient_id"] or "не указан"
        lines.append(f"{pnum}. {p_name} — ОМС {p_id}")

        tasks = patient["tasks"]
        multi = len(tasks) > 1
        for t_idx, task in enumerate(tasks):
            letter = f"{TASK_LETTERS[t_idx]}) " if multi else ""
            short = TASK_SHORT.get(task["task_number"], task["title"])
            text = _short_text(task)
            if text:
                base = f"   {TASK_MARKER} {letter}{short}: {text}"
            else:
                base = f"   {TASK_MARKER} {letter}{short}"
            lines.append(base + TASK_SUFFIX)
        lines.append("")

    poem = pick_poem(target_date)

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
