import os
import json
import gspread
from google.oauth2.service_account import Credentials

print("BOTON: начинаем проверку Google Sheets")

# Получаем секрет из GitHub
service_account_info = json.loads(
    os.environ["GOOGLE_SERVICE_ACCOUNT"]
)

# Разрешения
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

credentials = Credentials.from_service_account_info(
    service_account_info,
    scopes=scopes
)

client = gspread.authorize(credentials)

# ID вашей таблицы
SPREADSHEET_ID = "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"

# Открываем таблицу
spreadsheet = client.open_by_key(SPREADSHEET_ID)

print("Таблица найдена:", spreadsheet.title)

# Открываем Лист1
sheet = spreadsheet.worksheet("Лист1")

# Получаем данные
data = sheet.get_all_values()

print("Лист:", sheet.title)
print("Количество строк:", len(data))
print("Количество столбцов:", len(data[0]) if data else 0)

print("BOTON: Google Sheets успешно подключена!")
