import asyncio
import logging
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict

from settings import settings

logger = logging.getLogger(__name__)

# Throttle: max 1 concurrent SMTP connection + 30-second subject dedup
_EMAIL_SEMAPHORE = asyncio.Semaphore(1)
_RECENT_SUBJECTS: Dict[str, float] = {}
_EMAIL_DEDUP_SECONDS = 30

async def init_email_session() -> None:
    pass

async def close_email_session() -> None:
    pass

def format_drmt_email(subject: str, raw_body: str, html_body: str | None = None) -> tuple[str, str]:
    """
    Formats the email in a clean structure:
      Hello,

      {actual content / table}

      With regards,
      Team Quantivo
    """
    body_text = raw_body.strip()

    # If explicit html_body is passed, use it directly for HTML view
    if html_body:
        plain_text = f"Hello,\n\n{body_text}\n\nWith regards,\nTeam Quantivo\n"
        html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222222; margin: 24px; max-width: 850px;">
  <p style="margin: 0 0 16px 0;">Hello,</p>
  <div style="margin: 0 0 20px 0; color: #111111;">
    {html_body}
  </div>
  <p style="margin: 24px 0 0 0;">With regards,<br><strong>Team Quantivo</strong></p>
</body>
</html>
"""
        return plain_text, html_content

    # If raw_body already contains an HTML table, wrap it cleanly
    if "<table" in body_text.lower():
        plain_text = f"Hello,\n\n{body_text}\n\nWith regards,\nTeam Quantivo\n"
        html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222222; margin: 24px; max-width: 850px;">
  <p style="margin: 0 0 16px 0;">Hello,</p>
  <div style="margin: 0 0 20px 0; color: #111111;">
    {body_text}
  </div>
  <p style="margin: 24px 0 0 0;">With regards,<br><strong>Team Quantivo</strong></p>
</body>
</html>
"""
        return plain_text, html_content

    # Strip any existing greeting or sign-off to prevent duplication
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    cleaned_lines = []
    for l in lines:
        if l.lower() in ("hello,", "hello", "hi,", "hi", "with regards,", "with regards", "team quantivo"):
            continue
        cleaned_lines.append(l)

    clean_body = "\n".join(cleaned_lines)

    # Plain text format
    plain_text = f"Hello,\n\n{clean_body}\n\nWith regards,\nTeam Quantivo\n"

    # Simple text lines with <br>
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

def _build_env_subject(subject: str) -> str:
    env_str = getattr(settings, "ENVIRONMENT", "LOCAL").upper().strip()
    tag = f"[{env_str}]"
    if not subject.strip().startswith(tag):
        return f"{tag} {subject.strip()}"
    return subject.strip()

async def send_email_notification(subject: str, body: str, html_body: str | None = None) -> None:
    """Dispatches general alert / success email via SMTP with environment tagging."""
    await send_error_email(subject, body, html_body=html_body)

async def send_error_email(subject: str, body: str, html_body: str | None = None) -> None:
    """Dispatches simple, structured alert email via SMTP.

    Throttled: max 1 concurrent SMTP connection; duplicate subjects
    are suppressed for _EMAIL_DEDUP_SECONDS seconds.
    """
    subject = _build_env_subject(subject)
    # Dedup: skip if same subject was sent recently
    now = time.monotonic()
    last_sent = _RECENT_SUBJECTS.get(subject, 0.0)
    if now - last_sent < _EMAIL_DEDUP_SECONDS:
        return

    logger.debug("SMTP Email notification triggered")
    try:
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

        plain_text, html_content = format_drmt_email(subject, body, html_body=html_body)

        # Throttle: only 1 SMTP connection at a time
        async with _EMAIL_SEMAPHORE:
            # Re-check dedup after acquiring semaphore (another coroutine may have just sent)
            now = time.monotonic()
            if now - _RECENT_SUBJECTS.get(subject, 0.0) < _EMAIL_DEDUP_SECONDS:
                logger.debug(f"SMTP dedup (post-lock): suppressing '{subject}'")
                return

            _RECENT_SUBJECTS[subject] = now
            logger.debug("SMTP email provider request started")
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
            logger.debug("Email notification sent successfully via SMTP")

    except Exception as e:
        logger.error(f"Email notification failed: {e}")


async def send_auth_failed_email(broker: str, client_id: str, username: str, reason: str, user_email: str = "") -> None:
    """Dispatches failure email when credentials fail authentication."""
    subject = f"Authentication Failed: {broker}:{client_id}"
    plain_body = (
        f"Authentication failed for account {broker}:{client_id} ({username}).\n\n"
        f"Reason: {reason}\n\n"
        f"Please update the credentials in your Dropbox credentials CSV or static folder.\n"
        f"The DRMT system will automatically detect the changes within 1–2 minutes and verify the account."
    )
    email_row = f"<tr><td style='padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;'>User Email</td><td style='padding: 6px 12px; border: 1px solid #ddd;'>{user_email}</td></tr>" if user_email else ""
    html_body = f"""
    <div style="font-family: sans-serif; font-size: 14px; color: #222;">
      <h3 style="color: #d32f2f; margin-top: 0;">❌ Authentication Failed</h3>
      <table style="border-collapse: collapse; width: 100%; max-width: 600px; margin-bottom: 16px;">
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd; width: 140px;">Broker</td><td style="padding: 6px 12px; border: 1px solid #ddd;">{broker}</td></tr>
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;">Client ID</td><td style="padding: 6px 12px; border: 1px solid #ddd;">{client_id}</td></tr>
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;">Username</td><td style="padding: 6px 12px; border: 1px solid #ddd;">{username}</td></tr>
        {email_row}
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;">Failure Reason</td><td style="padding: 6px 12px; border: 1px solid #ddd; color: #d32f2f;">{reason}</td></tr>
      </table>
      <p style="color: #555;">Please update the credentials in your Dropbox credentials CSV or static folder. The DRMT background recheck worker will automatically detect changes within 1–2 minutes and verify the account.</p>
    </div>
    """
    await send_error_email(subject, plain_body, html_body=html_body)


async def send_credentials_restored_email(broker: str, client_id: str, username: str, user_email: str = "") -> None:
    """Dispatches confirmation email when updated credentials are authenticated successfully."""
    subject = f"Credentials Restored: {broker}:{client_id}"
    plain_body = (
        f"Credentials for account {broker}:{client_id} ({username}) have been verified successfully!\n\n"
        f"The account has been removed from the Block list and live position scanning is now resumed."
    )
    email_row = f"<tr><td style='padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;'>User Email</td><td style='padding: 6px 12px; border: 1px solid #ddd;'>{user_email}</td></tr>" if user_email else ""
    html_body = f"""
    <div style="font-family: sans-serif; font-size: 14px; color: #222;">
      <h3 style="color: #2e7d32; margin-top: 0;">✅ Credentials Verified & Restored</h3>
      <table style="border-collapse: collapse; width: 100%; max-width: 600px; margin-bottom: 16px;">
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd; width: 140px;">Broker</td><td style="padding: 6px 12px; border: 1px solid #ddd;">{broker}</td></tr>
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;">Client ID</td><td style="padding: 6px 12px; border: 1px solid #ddd;">{client_id}</td></tr>
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;">Username</td><td style="padding: 6px 12px; border: 1px solid #ddd;">{username}</td></tr>
        {email_row}
        <tr><td style="padding: 6px 12px; font-weight: bold; background: #f5f5f5; border: 1px solid #ddd;">Status</td><td style="padding: 6px 12px; border: 1px solid #ddd; color: #2e7d32; font-weight: bold;">Active / Working</td></tr>
      </table>
      <p style="color: #2e7d32; font-weight: bold;">Account unblocked. Live position scanning has resumed successfully.</p>
    </div>
    """
    await send_email_notification(subject, plain_body, html_body=html_body)
