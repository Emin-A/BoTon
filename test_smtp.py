"""Тест SMTP — отправляет простое письмо без вложений."""
import os, ssl, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def main():
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)
    to_email = os.environ.get("TEST_EMAIL", "")

    print(f"SMTP_HOST:       {host}")
    print(f"SMTP_PORT:       {port}")
    print(f"SMTP_USER:       {user}")
    print(f"SMTP_FROM_EMAIL: {from_email}")
    print(f"TEST_EMAIL:      {to_email}")

    if not all([host, user, password, to_email]):
        print("❌ Не все параметры заданы!")
        return

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = "BoTon SMTP тест"
    msg.attach(MIMEText("Если вы видите это письмо — SMTP работает корректно.", "plain", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
        print("✅ Письмо отправлено успешно!")
    except Exception as exc:
        print(f"❌ Ошибка: {exc}")


if __name__ == "__main__":
    main()
