import os
import json
import smtplib
from email.message import EmailMessage

import gspread
from google.oauth2.service_account import Credentials


def connect_google():
    service_account_info = json.loads(
        os.environ["GOOGLE_SERVICE_ACCOUNT"]
    )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )

    return gspread.authorize(credentials)


def test_smtp():
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ["SMTP_FROM_EMAIL"]

    print("Проверяем SMTP Яндекса...")
    print(f"SMTP сервер: {host}")
    print(f"SMTP порт: {port}")
    print(f"Отправитель: {from_email}")

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = from_email
    message["Subject"] = "BOTON — тест SMTP"
    message.set_content(
        "Тестовое письмо от Boton.\n\n"
        "Если это письмо пришло, SMTP через Яндекс работает."
    )

    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)

    print("SMTP: авторизация успешна.")
    print("SMTP: тестовое письмо отправлено.")


def test_google():
    client = connect_google()

    spreadsheet = client.open_by_key(
        "13maufkGFPUdNE-d_i_nvC4l5xf3ParmuFCNYJ32wmVk"
    )

    sheet = spreadsheet.worksheet("Лист1")

    print("Google Sheets: подключение успешно.")
    print(f"Таблица: {spreadsheet.title}")
    print(f"Строк: {len(sheet.get_all_values())}")


if __name__ == "__main__":
    print("=" * 50)
    print("BOTON — ТЕСТ ПОДКЛЮЧЕНИЙ")
    print("=" * 50)

    test_google()
    test_smtp()

    print("=" * 50)
    print("ТЕСТ УСПЕШНО ЗАВЕРШЁН")
    print("Таблица не изменялась.")
    print("Врачам письма не отправлялись.")
    print("=" * 50)
