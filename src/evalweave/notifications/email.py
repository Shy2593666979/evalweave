import smtplib
import ssl
from email.message import EmailMessage

from evalweave.core.config import get_settings


def send_email(subject: str, content: str, recipient: str) -> str | None:
    config = get_settings().notifications.email
    if not config.enabled or not config.host or not config.from_address:
        raise RuntimeError("Email notifications are not configured")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.from_address
    message["To"] = recipient
    message.set_content(content)
    context = ssl.create_default_context()
    if config.use_ssl:
        with smtplib.SMTP_SSL(
            config.host, config.port, timeout=config.timeout_seconds, context=context
        ) as client:
            if config.username:
                client.login(config.username, config.password)
            client.send_message(message)
    else:
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds) as client:
            if config.starttls:
                client.starttls(context=context)
            if config.username:
                client.login(config.username, config.password)
            client.send_message(message)
    return message.get("Message-ID")
