"""Small SMTP adapter; tests can replace send_email without network access."""
import asyncio
import os
import smtplib
from email.message import EmailMessage


def _setting(name: str, fallback: str = "") -> str:
    return os.getenv(name) or os.getenv(fallback, "")


def _send_sync(to: str, subject: str, body: str) -> None:
    gmail_user = _setting("CLEARLINE_APP_USER")
    host = _setting("SMTP_HOST") or _setting("SMTP")
    if not host and gmail_user:
        host = "smtp.gmail.com"
    port = int(_setting("SMTP_PORT") or "587")
    user = _setting("SMTP_USER", "CLEARLINE_APP_USER")
    password = _setting("SMTP_PASSWORD", "CLEARLINE_APP_PASS")
    sender = _setting("EMAIL_FROM") or user
    if not host or (not sender and not gmail_user):
        raise RuntimeError("Email delivery is not configured")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


async def send_email(to: str, subject: str, body: str) -> None:
    await asyncio.to_thread(_send_sync, to, subject, body)
