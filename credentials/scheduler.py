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


def _get_market_close_time() -> tuple[int, int]:
    h = getattr(settings, "MARKET_CLOSE_HOUR", 15)
    m = getattr(settings, "MARKET_CLOSE_MINUTE", 30)
    return int(h), int(m)


def get_seconds_until_next_market_close() -> float:
    """Calculates seconds until the next 03:30 PM IST market close on a trading weekday."""
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(_IST)
    th, tm = _get_market_close_time()

    target_ist = now_ist.replace(hour=th, minute=tm, second=0, microsecond=0)
    if now_ist >= target_ist:
        target_ist += timedelta(days=1)

    # Skip weekends (Saturday=5, Sunday=6)
    while target_ist.weekday() >= 5:
        target_ist += timedelta(days=1)

    delta_seconds = (target_ist - now_ist).total_seconds()
    return max(1.0, delta_seconds)


def build_market_close_report_html(successful_accounts: list[dict], failed_accounts: list[dict]) -> tuple[str, str]:
    """Builds distinct tabular report for successful and failed accounts at market close."""
    import html
    total_acc = len(successful_accounts) + len(failed_accounts)
    now_ist_str = datetime.now(timezone.utc).astimezone(_IST).strftime("%Y-%m-%d %I:%M %p IST")

    plain_parts = [
        f"Market-Close Authentication & Credential Report ({now_ist_str})",
        f"Total Configured: {total_acc} | Successful: {len(successful_accounts)} | Failed: {len(failed_accounts)}",
        ""
    ]

    html_parts = [
        f"""<div style="margin-bottom: 16px; border-bottom: 2px solid #e5e7eb; padding-bottom: 12px;">
  <h2 style="margin: 0 0 6px 0; color: #111827; font-size: 18px; font-weight: 700;">📊 Market-Close Authentication & Credential Report</h2>
  <div style="font-size: 13px; color: #6b7280;">Session closed at {now_ist_str}. Summary across all configured accounts:</div>
  <div style="margin-top: 10px;">
    <span style="display: inline-block; padding: 4px 10px; background: #f3f4f6; border-radius: 4px; font-size: 12px; font-weight: 600; color: #374151; margin-right: 8px;">Total: {total_acc}</span>
    <span style="display: inline-block; padding: 4px 10px; background: #ecfdf5; border-radius: 4px; font-size: 12px; font-weight: 600; color: #059669; margin-right: 8px;">Successful: {len(successful_accounts)}</span>
    <span style="display: inline-block; padding: 4px 10px; background: #fef2f2; border-radius: 4px; font-size: 12px; font-weight: 600; color: #dc2626;">Failed: {len(failed_accounts)}</span>
  </div>
</div>"""
    ]

    # ── Table 1: Successful Accounts ──
    plain_parts.append("\n=== Successful Accounts ===")
    plain_parts.append(f"{'Broker':<12} | {'Client ID':<12} | {'User Name':<22} | {'User Email':<28} | Status")
    plain_parts.append("-" * 90)

    html_parts.append(f"""<div style="margin-top: 20px; margin-bottom: 8px; font-size: 15px; font-weight: bold; color: #065f46;">
  ✅ Successful Authentications ({len(successful_accounts)})
</div>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #d1d5db; font-size: 12.5px; margin-bottom: 20px;">
  <thead>
    <tr style="background-color: #f9fafb; text-align: left; color: #374151;">
      <th style="padding: 8px; border: 1px solid #d1d5db;">Broker</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">Client ID</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">User Name</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">User Email</th>
      <th style="padding: 8px; border: 1px solid #d1d5db; text-align: center;">Status</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">Last Updated</th>
    </tr>
  </thead>
  <tbody>""")

    if not successful_accounts:
        html_parts.append('<tr><td colspan="6" style="padding: 12px; text-align: center; color: #6b7280;">No active successful accounts.</td></tr>')
        plain_parts.append("No active successful accounts.")
    else:
        for acc in successful_accounts:
            b = html.escape(str(acc.get("broker", "")))
            cid = html.escape(str(acc.get("client_id", "")))
            un = html.escape(str(acc.get("username", "")))
            em = html.escape(str(acc.get("user_email", "")))
            up = html.escape(str(acc.get("token_updated_at", "") or "Session Valid"))
            plain_parts.append(f"{acc.get('broker',''):<12} | {acc.get('client_id',''):<12} | {acc.get('username',''):<22} | {acc.get('user_email',''):<28} | Active")
            html_parts.append(f"""<tr>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; font-weight: 600;">{b}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; font-family: monospace;">{cid}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db;">{un}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db;">{em}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; text-align: center; color: #059669; font-weight: 600;">✅ Active</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; color: #6b7280; font-size: 11.5px;">{up}</td>
    </tr>""")
    html_parts.append("  </tbody>\n</table>")

    # ── Table 2: Failed Accounts ──
    plain_parts.append("\n=== Failed / Blocked Accounts ===")
    plain_parts.append(f"{'Broker':<12} | {'Client ID':<12} | {'User Name':<22} | {'User Email':<28} | Reason")
    plain_parts.append("-" * 90)

    html_parts.append(f"""<div style="margin-top: 24px; margin-bottom: 8px; font-size: 15px; font-weight: bold; color: #991b1b;">
  ❌ Failed / Blocked Authentications ({len(failed_accounts)})
</div>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #d1d5db; font-size: 12.5px; margin-bottom: 20px;">
  <thead>
    <tr style="background-color: #fef2f2; text-align: left; color: #991b1b;">
      <th style="padding: 8px; border: 1px solid #d1d5db;">Broker</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">Client ID</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">User Name</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">User Email</th>
      <th style="padding: 8px; border: 1px solid #d1d5db; text-align: center;">Status</th>
      <th style="padding: 8px; border: 1px solid #d1d5db;">Failure Reason</th>
    </tr>
  </thead>
  <tbody>""")

    if not failed_accounts:
        html_parts.append('<tr><td colspan="6" style="padding: 12px; text-align: center; color: #059669; font-weight: 600;">🎉 All accounts authenticated successfully. Zero failures.</td></tr>')
        plain_parts.append("All accounts authenticated successfully. Zero failures.")
    else:
        for acc in failed_accounts:
            b = html.escape(str(acc.get("broker", "")))
            cid = html.escape(str(acc.get("client_id", "")))
            un = html.escape(str(acc.get("username", "")))
            em = html.escape(str(acc.get("user_email", "")))
            re = html.escape(str(acc.get("reason", "Authentication failed")))
            plain_parts.append(f"{acc.get('broker',''):<12} | {acc.get('client_id',''):<12} | {acc.get('username',''):<22} | {acc.get('user_email',''):<28} | {acc.get('reason','')}")
            html_parts.append(f"""<tr>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; font-weight: 600;">{b}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; font-family: monospace;">{cid}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db;">{un}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db;">{em}</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; text-align: center; color: #dc2626; font-weight: 600;">❌ Failed</td>
      <td style="padding: 7px 8px; border: 1px solid #d1d5db; color: #b91c1c; font-size: 11.5px;">{re}</td>
    </tr>""")
    html_parts.append("  </tbody>\n</table>")

    return "\n".join(plain_parts).strip(), "\n".join(html_parts).strip()


async def execute_market_close_email_report() -> dict:
    """Collects all configured user authentication states and sends the market-close report."""
    from database.db import get_db
    from position.blocklist import get_blocked_users, is_blocked
    from utils.user_mapping import get_client_username
    from credentials.dynamic_loader import get_client_email
    from utils.email import send_email_notification, send_error_email

    await logger_system.info("📊 [MARKET CLOSE] Generating end-of-day authentication & credential report...")

    async with get_db() as db:
        db.row_factory = None
        query = """
            SELECT broker, client_id, status, token, token_updated_at
            FROM credentials
            WHERE UPPER(broker) != 'FAKE'
              AND client_id IS NOT NULL
              AND TRIM(client_id) != ''
            ORDER BY broker, client_id
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()

    blocked_dict = {str(b.get("client_id", "")).strip(): b for b in get_blocked_users()}

    successful_accounts = []
    failed_accounts = []

    for r in rows:
        broker = r[0]
        cid = str(r[1]).strip()
        db_status = r[2]
        token = r[3]
        token_updated = r[4]
        username = get_client_username(cid)
        email = get_client_email(cid)

        if is_blocked(cid):
            reason = blocked_dict.get(cid, {}).get("reason", "Blocked / Authentication failed")
            failed_accounts.append({
                "broker": broker,
                "client_id": cid,
                "username": username,
                "user_email": email,
                "reason": reason
            })
        elif db_status == "working" and token:
            successful_accounts.append({
                "broker": broker,
                "client_id": cid,
                "username": username,
                "user_email": email,
                "token_updated_at": token_updated
            })
        else:
            failed_accounts.append({
                "broker": broker,
                "client_id": cid,
                "username": username,
                "user_email": email,
                "reason": f"Status '{db_status}' without active token"
            })

    plain_report, html_report = build_market_close_report_html(successful_accounts, failed_accounts)

    th, tm = _get_market_close_time()
    time_label = _format_target_time_str(th, tm)
    subject = f"Market-Close Authentication Report: {len(successful_accounts)} Successful, {len(failed_accounts)} Failed ({time_label})"
    try:
        if failed_accounts:
            await send_error_email(subject, plain_report, html_body=html_report)
        else:
            await send_email_notification(subject, plain_report, html_body=html_report)
        await logger_system.info(f"📧 [MARKET CLOSE] Dispatched market-close report email ({len(successful_accounts)} successful, {len(failed_accounts)} failed).")
    except Exception as e:
        await logger_system.error(f"❌ [MARKET CLOSE] Failed sending report email: {e}")

    return {
        "successful_count": len(successful_accounts),
        "failed_count": len(failed_accounts),
        "total_count": len(successful_accounts) + len(failed_accounts),
        "successful_accounts": successful_accounts,
        "failed_accounts": failed_accounts
    }


async def start_market_close_report_scheduler():
    """Background worker that sleeps until 03:30 PM (15:30 IST) every market trading day."""
    th, tm = _get_market_close_time()
    time_label = _format_target_time_str(th, tm)
    await logger_system.info(f"⏰ [SCHEDULER] Market-Close Email Report Worker initialized ({time_label}).")
    while True:
        try:
            wait_seconds = get_seconds_until_next_market_close()
            now_ist = datetime.now(timezone.utc).astimezone(_IST)
            next_run_ist = now_ist + timedelta(seconds=wait_seconds)
            hours = int(wait_seconds // 3600)
            minutes = int((wait_seconds % 3600) // 60)
            logger.info(
                f"⏰ [SCHEDULER] Next Market-Close report scheduled in {hours}h {minutes}m "
                f"at {next_run_ist.strftime('%Y-%m-%d %H:%M:%S IST')}."
            )
            await asyncio.sleep(wait_seconds)
            await execute_market_close_email_report()
            await asyncio.sleep(60.0)  # Safety sleep to advance clock past target
        except asyncio.CancelledError:
            logger.info("🛑 [SCHEDULER] Market-close report worker stopped.")
            break
        except Exception as e:
            logger.error(f"❌ [SCHEDULER] Error in market-close scheduler loop: {e}")
            await asyncio.sleep(60.0)

