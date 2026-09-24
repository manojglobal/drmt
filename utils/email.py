import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

async def init_email_session() -> None:
    pass

async def close_email_session() -> None:
    pass

def format_drmt_email(subject: str, raw_body: str) -> tuple[str, str]:
    """
    Formats the email in a simple, clean structure without tables:
      Hello,

      {actual error message / details}

      With regards,
      Team Quantivo
    """
    body_text = raw_body.strip()
    
    # Strip any existing greeting or sign-off to prevent duplication
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    cleaned_lines = []
    for l in lines:
        if l.lower() in ("hello,", "hello", "hi,", "hi", "with regards,", "with regards", "team quantivo"):
            continue
        cleaned_lines.append(l)
    
    clean_body = "\n".join(cleaned_lines)
    
    # Plain text format strictly matching requirements
    plain_text = f"Hello,\n\n{clean_body}\n\nWith regards,\nTeam Quantivo\n"
    
    # Simple, clean HTML without any tables
    html_lines = "<br>\n".join(cleaned_lines)
    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222222; margin: 24px; max-width: 650px;">
  <p style="margin: 0 0 16px 0;">Hello,</p>
  <div style="margin: 0 0 20px 0; color: #111111;">
    {html_lines}
  </div>
  <p style="margin: 24px 0 0 0;">With regards,<br><strong>Team Quantivo</strong></p>
</body>
</html>
"""
    return plain_text, html_content

def _send_email_sync(subject: str, plain_body: str, html_body: str, email_to: str, email_from: str, host: str, port: int, user: str, password: str):
    msg = MIMEMultipart("alternative")
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    server = smtplib.SMTP(host, port, timeout=10)
    try:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    finally:
        server.quit()

async def send_error_email(subject: str, body: str) -> None:
    """Dispatches simple, structured alert email via SMTP."""
    logger.info("SMTP Email notification triggered")
    try:
        from settings import settings

        email_to = settings.EMAIL_TO.strip()
        email_from = settings.EMAIL_FROM.strip()
        host = settings.EMAIL_HOST.strip()
        port = settings.EMAIL_PORT
        user = settings.EMAIL_USER.strip()
        password = settings.EMAIL_PASS.strip()

        if not user or not password:
            logger.warning("Email notification failed: SMTP credentials are missing in .env")
            return

        if not email_to:
            logger.warning("Email notification failed: EMAIL_TO is not set in .env")
            return

        plain_text, html_content = format_drmt_email(subject, body)

        logger.info("SMTP email provider request started")
        await asyncio.to_thread(
            _send_email_sync,
            subject,
            plain_text,
            html_content,
            email_to,
            email_from,
            host,
            port,
            user,
            password
        )
        logger.info("Email notification sent successfully via SMTP")

    except Exception as e:
        logger.error(f"Email notification failed: {e}")
