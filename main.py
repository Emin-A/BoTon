import os
import json
import gspread
from google.oauth2.service_account import Credentials

print("BOTON: начинаем проверку двух таблиц")

service_account_info = json.loads(
    os.environ["GOOGLE_SERVICE_ACCOUNT"]
)

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

credentials = Credentials.from_service_account_info(
    service_account_info,
    scopes=scopes
)

client = gspread.authorize(credentials)

# ─────────────────────────────────────────────
# 1. Таблица задач
# ─────────────────────────────────────────────

TASKS_SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"

tasks_spreadsheet = client.open_by_key(TASKS_SPREADSHEET_ID)
tasks_sheet = tasks_spreadsheet.worksheet("Лист1")
tasks_data = tasks_sheet.get_all_values()

print()
print("ТАБЛИЦА ЗАДАЧ")
print("Название:", tasks_spreadsheet.title)
print("Лист:", tasks_sheet.title)
print("Строк:", len(tasks_data))
print("Столбцов:", len(tasks_data[0]) if tasks_data else 0)

# ─────────────────────────────────────────────
# 2. База врачей
# ─────────────────────────────────────────────

DOCTORS_SPREADSHEET_ID = "1grwcVilkiwOI01UgWu4jrhOLV2GVM7TJ6TqlXE7enAU"

doctors_spreadsheet = client.open_by_key(DOCTORS_SPREADSHEET_ID)
doctors_sheet = doctors_spreadsheet.worksheet("Врачи")
doctors_data = doctors_sheet.get_all_values()

print()
print("БАЗА ВРАЧЕЙ")
print("Название:", doctors_spreadsheet.title)
print("Лист:", doctors_sheet.title)
print("Строк:", len(doctors_data))
print("Столбцов:", len(doctors_data[0]) if doctors_data else 0)

# ─────────────────────────────────────────────
# Проверяем структуру первых строк
# ─────────────────────────────────────────────

print()
print("ПЕРВАЯ СТРОКА БАЗЫ ВРАЧЕЙ:")
print(doctors_data[0] if doctors_data else "пусто")

if len(doctors_data) > 1:
    print()
    print("ВТОРАЯ СТРОКА БАЗЫ ВРАЧЕЙ:")
    print(doctors_data[1])

print()
print("BOTON: обе таблицы успешно подключены!")
