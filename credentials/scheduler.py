import asyncio
from datetime import datetime, timezone, timedelta
import logging
import time

from logger.logger import logger_system, logger_auth
from settings import settings

logger = logging.getLogger("DRMT.Scheduler")

# Indian Standard Time (UTC+05:30)
_IST = timezone(timedelta(hours=5, minutes=30))


def _get_target_time() -> tuple[int, int]:
    h = getattr(settings, "DAILY_REFRESH_HOUR", 9)
    m = getattr(settings, "DAILY_REFRESH_MINUTE", 10)
    return int(h), int(m)


def _format_target_time_str(h: int, m: int) -> str:
    dt = datetime(2026, 1, 1, h, m)
    return dt.strftime("%I:%M %p IST").lstrip("0")


def get_seconds_until_next_ist_time(target_hour: int | None = None, target_minute: int | None = None, target_second: int = 0) -> float:
    """Calculates the exact number of seconds until the next target IST time."""
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(_IST)

    if target_hour is None or target_minute is None:
        th, tm = _get_target_time()
        target_hour = th if target_hour is None else target_hour
        target_minute = tm if target_minute is None else target_minute

    target_ist = now_ist.replace(hour=target_hour, minute=target_minute, second=target_second, microsecond=0)
    if now_ist >= target_ist:
        # Target time already passed today; schedule for tomorrow
        target_ist += timedelta(days=1)

    delta_seconds = (target_ist - now_ist).total_seconds()
    return max(1.0, delta_seconds)


def build_refresh_tables_html(auth_results: list[dict]) -> tuple[str, str]:
    """
    Builds broker-grouped tabular status tables matching the format from email_massage.png:
    - [Broker] Access Token Status heading
    - Table columns: User Email | Access Token Refreshed (✅/❌) | Reason
    Returns (plain_text, html_body).
    """
    import html

    # Group accounts by broker
    by_broker: dict[str, list[dict]] = {}
    for item in auth_results:
        b = str(item.get("broker") or "UNKNOWN").strip().upper()
        by_broker.setdefault(b, []).append(item)

    def format_broker_name(raw: str) -> str:
        raw_u = raw.upper()
        if "MSTOCK" in raw_u:
            return "Mstock"
        if "FLATTRADE" in raw_u:
            return "Flattrade"
        if "ALICE" in raw_u:
            return "Alice Blue"
        if "FYERS" in raw_u:
            return "Fyers"
        if "KOTAK" in raw_u:
            return "Kotak Neo"
        return raw.title()

    html_parts = []
    plain_parts = []

    for broker_key, accounts in by_broker.items():
        broker_title = format_broker_name(broker_key)

        # Plain text representation
        plain_parts.append(f"\n{broker_title} Access Token Status")
        plain_parts.append(f"{'User Email':<32} | {'Access Token Refreshed':<22} | Reason")
        plain_parts.append("-" * 75)
        for acc in accounts:
            ok = acc.get("refreshed", False)
            email_val = acc.get("user_email", "")
            icon = "✅" if ok else "❌"
            reason_val = acc.get("reason", "") if not ok else ""
            plain_parts.append(f"{email_val:<32} | {icon:^22} | {reason_val}")

        # HTML representation matching email_massage.png
        table_html = f"""
<div style="margin-top: 24px; margin-bottom: 8px; font-size: 15px; font-weight: bold; color: #222222; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
  {html.escape(broker_title)} Access Token Status
</div>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #777777; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 13px; margin-bottom: 24px;">
  <thead>
    <tr style="background-color: #f2f2f2; text-align: left;">
      <th style="border: 1px solid #777777; padding: 8px 12px; font-weight: bold; width: 35%;">User Email</th>
      <th style="border: 1px solid #777777; padding: 8px 12px; font-weight: bold; text-align: center; width: 22%;">Access Token Refreshed</th>
      <th style="border: 1px solid #777777; padding: 8px 12px; font-weight: bold; width: 43%;">Reason</th>
    </tr>
  </thead>
  <tbody>
"""
        for acc in accounts:
            ok = acc.get("refreshed", False)
            email_val = html.escape(str(acc.get("user_email", "")))
            if ok:
                icon_html = '<span style="font-size: 16px;">&#9989;</span>'
                reason_html = ""
            else:
                icon_html = '<span style="font-size: 16px; color: #d32f2f;">&#10060;</span>'
                raw_reason = str(acc.get("reason", "Failed"))
                reason_html = html.escape(raw_reason)

            table_html += f"""    <tr>
      <td style="border: 1px solid #777777; padding: 8px 12px; color: #111111;">{email_val}</td>
      <td style="border: 1px solid #777777; padding: 8px 12px; text-align: center;">{icon_html}</td>
      <td style="border: 1px solid #777777; padding: 8px 12px; color: #333333;">{reason_html}</td>
    </tr>\n"""

        table_html += "  </tbody>\n</table>"
        html_parts.append(table_html)

    plain_output = "\n".join(plain_parts).strip()
    html_output = "\n".join(html_parts).strip()
    return plain_output, html_output


async def execute_morning_credential_refresh():
    """Executes the daily scheduled credential and token sync routine."""
    t0 = time.monotonic()
    th, tm = _get_target_time()
    time_label = _format_target_time_str(th, tm)
    await logger_system.info(f"🌅 [DAILY REFRESH] {time_label} ➔ Starting credential sync & token generation...")
    await logger_auth.info("🌅 [DAILY REFRESH] Pulling latest credentials from remote storage...")

    # 1. Pull latest credentials from Dropbox CSV and sync to SQLite
    from credentials.dynamic_loader import fetch_and_sync_credentials
    try:
        await fetch_and_sync_credentials()
        await logger_auth.info("📥 [DAILY REFRESH] Successfully re-synchronized credentials from remote CSV.")
    except Exception as e:
        await logger_auth.error(f"❌ [DAILY REFRESH] Error fetching remote credentials: {e}")

    # 2. Reset previous day's Block list for the new trading day
    from position.blocklist import clear_blocklist
    cleared_count = clear_blocklist()
    await logger_auth.info(f"🧹 [DAILY REFRESH] Reset Block list ({cleared_count} old entries cleared) for the new trading day.")

    # 3. Authenticate configured broker accounts to generate fresh session tokens
    from main import authenticate_configured_credentials
    from utils.email import send_email_notification, send_error_email
    succeeded = []
    failed = []
    auth_results = []
    try:
        res = await authenticate_configured_credentials()
        if len(res) == 3:
            succeeded, failed, auth_results = res
        else:
            succeeded, failed = res[:2]
    except Exception as e:
        await logger_auth.error(f"❌ [DAILY REFRESH] Error during morning authentication: {e}")
        failed.append(("SYSTEM", f"Authentication execution exception: {e}"))

    elapsed = time.monotonic() - t0
    total_acc = len(succeeded) + len(failed)
    await logger_system.info(
        f"✅ [DAILY REFRESH] Completed in {elapsed:.1f}s. "
        f"{len(succeeded)} succeeded, {len(failed)} failed out of {total_acc} account(s). Ready for 9:15 AM market open."
    )

    # 4. Dispatch Tabular Email Notification (Matching email_massage.png format)
    plain_table, html_table = build_refresh_tables_html(auth_results)
    if failed:
        try:
            await send_error_email(
                f"Daily Credential Refresh Alert: {len(failed)} Account(s) Failed ({time_label})",
                plain_table,
                html_body=html_table,
            )
            await logger_auth.info("📧 [DAILY REFRESH] Tabular failure alert email sent.")
        except Exception as mail_err:
            await logger_auth.error(f"❌ [DAILY REFRESH] Failed sending failure alert email: {mail_err}")
    elif succeeded:
        try:
            await send_email_notification(
                f"Daily Credential Refresh Successful ({time_label})",
                plain_table,
                html_body=html_table,
            )
            await logger_auth.info("📧 [DAILY REFRESH] Tabular success notification email sent.")
        except Exception as mail_err:
            await logger_auth.error(f"❌ [DAILY REFRESH] Failed sending success notification email: {mail_err}")


async def start_daily_credential_scheduler():
    """Background worker that sleeps until configured IST time every day."""
    th, tm = _get_target_time()
    time_label = _format_target_time_str(th, tm)
    await logger_system.info(f"⏰ [SCHEDULER] Daily {time_label} Credential Refresh Worker initialized.")
    while True:
        try:
            th, tm = _get_target_time()
            time_label = _format_target_time_str(th, tm)
            wait_seconds = get_seconds_until_next_ist_time(target_hour=th, target_minute=tm, target_second=0)
            now_ist = datetime.now(timezone.utc).astimezone(_IST)
            next_run_ist = now_ist + timedelta(seconds=wait_seconds)

            hours = int(wait_seconds // 3600)
            minutes = int((wait_seconds % 3600) // 60)
            logger.info(
                f"⏰ [SCHEDULER] Next {time_label} refresh scheduled in {hours}h {minutes}m "
                f"at {next_run_ist.strftime('%Y-%m-%d %H:%M:%S IST')}."
            )

            # Sleep until target time
            await asyncio.sleep(wait_seconds)

            # Execute the morning routine
            await execute_morning_credential_refresh()

            # Safety sleep to ensure the clock has advanced past 09:10:00
            await asyncio.sleep(10.0)

        except asyncio.CancelledError:
            logger.info("🛑 [SCHEDULER] Daily credential refresh worker stopped.")
            break
        except Exception as e:
            logger.error(f"❌ [SCHEDULER] Error in daily scheduler loop: {e}")
            await asyncio.sleep(60.0)  # Retry check after 1 minute if error occurs
