# BoTon v3.3 — модульная структура

## Файлы
- `main.py` — точка входа.
- `config.py` — настройки и константы.
- `utils.py` — время, нормализация, разбор дат.
- `google_sheets.py` — Google Sheets API и пакетная запись.
- `log_store.py` — журнал `BoTon_Log`.
- `doctors.py` — справочник и сопоставление врачей.
- `tasks.py` — отбор и подготовка заданий.
- `mailer.py` — SMTP и формирование писем.
- `sender.py` — рассылка.
- `receiver.py` — обработка ответов IMAP.
- `report.py` — отчёт заведующему.

## Запуск
Секреты GitHub Actions и переменные окружения сохраняются. Команда запуска остаётся:
`python main.py`

## Важно
Перед боевым запуском сначала используйте `TEST_DOCTOR_DIAG = True` или `TEST_TO_RESERVE = True`.
В `config.py` режимы запуска выставлены в `False`.
