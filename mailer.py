def send_email(to_email, subject, body, html_body=None, attachments=None):
    """Отправка письма с поддержкой HTML и вложений."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("SMTP_FROM_EMAIL", user)

    if not to_email:
        raise RuntimeError("Пустой получатель")

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject

    if html_body:
        msg.set_content(body, subtype="plain", charset="utf-8")
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    else:
        msg.set_content(body, subtype="plain", charset="utf-8")

    # Добавляем вложения
    if attachments:
        for filename, content in attachments:
            msg.add_attachment(content, maintype="text", subtype="html", filename=filename)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
