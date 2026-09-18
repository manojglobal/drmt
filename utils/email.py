import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)

async def init_email_session() -> None:
    # No longer needed for aiohttp, just a stub to prevent breaking startup scripts
    pass

async def close_email_session() -> None:
    # No longer needed for aiohttp
    pass

def _send_email_sync(subject: str, body: str, email_to: str, email_from: str, host: str, port: int, user: str, password: str):
    msg = MIMEMultipart()
    msg['From'] = email_from
    msg['To'] = email_to
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    
    server = smtplib.SMTP(host, port, timeout=10)
    try:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    finally:
        server.quit()

async def send_error_email(subject: str, body: str) -> None:
    # Dispatches an alert email when a critical error occurs using SMTP.
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

        logger.info("SMTP email provider request started")
        
        # Run blocking SMTP calls in a background thread to prevent blocking the event loop
        await asyncio.to_thread(_send_email_sync, subject, body, email_to, email_from, host, port, user, password)
        
        logger.info("Email notification sent successfully via SMTP")

    except Exception as e:
        logger.error(f"Email notification failed: {e}")
