"""
credentials/watcher.py — Background Worker for Blocked Credentials Recovery.
Runs at a fixed interval of 1–2 minutes (default: 90s).
Syncs latest credentials from the source of truth (Dropbox CSV or static folder).
Compares credentials of blocked accounts:
- If unchanged (match): Remains in block list (0 broker calls).
- If changed (mismatch): Attempts authentication once.
- If successful: Removes from block list and dispatches confirmation email.
- If failed: Remains in block list and dispatches failure email.
"""

import asyncio
from datetime import datetime, timezone, timedelta
import logging
from typing import Optional

from settings import settings
from models.schemas import Credential
from position.blocklist import (
    get_blocked_users,
    get_blocked_user,
    update_blocked_user_signature,
    remove_from_blocklist,
    is_blocked
)
from credentials.dynamic_loader import (
    fetch_and_sync_credentials,
    get_dynamic_credentials,
    compute_credential_signature,
    get_client_email
)
from position.chronos import refresh_token_with_backoff
from utils.email import send_auth_failed_email, send_credentials_restored_email
from logger.logger import logger_auth, logger_system

logger = logging.getLogger("DRMT.CredentialsWatcher")

_IST = timezone(timedelta(hours=5, minutes=30))


def is_market_session_active(dt: Optional[datetime] = None) -> bool:
    """
    Returns True if the current or given time falls within the active market session:
    Monday to Friday, between 09:15 AM IST and 03:30 PM (15:30) IST.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt_ist = dt.replace(tzinfo=_IST)
    else:
        dt_ist = dt.astimezone(_IST)

    # Monday is 0, Sunday is 6. Weekends (Saturday=5, Sunday=6) are non-market days.
    if dt_ist.weekday() >= 5:
        return False

    open_h = getattr(settings, "MARKET_OPEN_HOUR", 9)
    open_m = getattr(settings, "MARKET_OPEN_MINUTE", 15)
    close_h = getattr(settings, "MARKET_CLOSE_HOUR", 15)
    close_m = getattr(settings, "MARKET_CLOSE_MINUTE", 30)

    market_open = dt_ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    market_close = dt_ist.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

    return market_open <= dt_ist < market_close


def get_seconds_until_next_market_session(dt: Optional[datetime] = None) -> float:
    """
    Calculates seconds until the next market session opens (09:15 AM IST on the next trading weekday).
    If currently before 09:15 AM IST on a weekday, returns seconds until 09:15 AM IST today.
    If currently during market session, returns 0.0.
    If currently after 03:30 PM (15:30) IST on a weekday or on a weekend, returns seconds until 09:15 AM IST next weekday.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt_ist = dt.replace(tzinfo=_IST)
    else:
        dt_ist = dt.astimezone(_IST)

    open_h = getattr(settings, "MARKET_OPEN_HOUR", 9)
    open_m = getattr(settings, "MARKET_OPEN_MINUTE", 15)
    close_h = getattr(settings, "MARKET_CLOSE_HOUR", 15)
    close_m = getattr(settings, "MARKET_CLOSE_MINUTE", 30)

    today_open = dt_ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    today_close = dt_ist.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

    if dt_ist.weekday() < 5 and today_open <= dt_ist < today_close:
        return 0.0

    if dt_ist.weekday() < 5 and dt_ist < today_open:
        target = today_open
    else:
        target = today_open + timedelta(days=1)

    while target.weekday() >= 5:
        target += timedelta(days=1)

    delta = (target - dt_ist).total_seconds()
    return max(1.0, delta)


async def run_blocked_credentials_check_iteration() -> int:
    """
    Executes a single check iteration for all currently blocked accounts against the source of truth.
    Returns the number of accounts successfully unblocked in this iteration.
    """
    blocked_list = get_blocked_users()
    if not blocked_list:
        return 0

    await logger_system.info(f"🔍 [CREDENTIALS WATCHER] Checking {len(blocked_list)} blocked account(s) against source of truth (Dropbox/static)...")

    # 1. Fetch latest credentials from source of truth (Dropbox or static CSV)
    try:
        await fetch_and_sync_credentials()
    except Exception as e:
        logger.error(f"❌ [CREDENTIALS WATCHER] Error syncing credentials: {e}")
        return 0

    unblocked_count = 0

    # 2. Inspect each blocked user
    for b_user in blocked_list:
        client_id = str(b_user.get("client_id") or "").strip()
        broker = str(b_user.get("broker") or "").strip().upper()
        username = str(b_user.get("username") or client_id)
        stored_sig = b_user.get("credential_signature") or ""

        if not is_blocked(client_id):
            continue

        # Get fresh credentials from memory (populated from source of truth)
        fresh_data = get_dynamic_credentials(client_id)
        if not fresh_data:
            logger.info(f"ℹ️ [CREDENTIALS WATCHER] {broker}:{client_id} not present in source of truth CSV. Keeping blocked.")
            continue

        new_sig = compute_credential_signature(fresh_data)
        if stored_sig and new_sig == stored_sig:
            logger.debug(
                f"ℹ️ [CREDENTIALS WATCHER] Credentials unchanged for blocked account {broker}:{client_id} ({username}). Skipping verification."
            )
            continue

        logger.info(
            f"🔄 [CREDENTIALS WATCHER] Checking credentials for {broker}:{client_id} ({username}) "
            f"against source of truth. Attempting verification..."
        )

        cred = Credential(
            broker=fresh_data.get("broker", broker),
            client_id=client_id,
            api_key=fresh_data.get("api_key", ""),
            api_secret=fresh_data.get("api_secret", ""),
            totp_secret=fresh_data.get("totp_secret", ""),
            password=fresh_data.get("password", ""),
            static_ip=fresh_data.get("static_ip", ""),
            fingerprint=f"{broker}:{client_id}",
            status="working",
            token=fresh_data.get("token", "")
        )

        user_email = get_client_email(client_id)

        try:
            ok = await refresh_token_with_backoff(cred)
            if ok:
                # ── SUCCESS ──
                unblocked_count += 1
                remove_from_blocklist(client_id)
                if fresh_data and cred.token:
                    fresh_data["token"] = cred.token
                await logger_auth.info(f"✅ [CREDENTIALS WATCHER] Verification succeeded for {broker}:{client_id} ({username}). Account unblocked!")
                
                # Dispatch success email confirmation
                asyncio.create_task(
                    send_credentials_restored_email(
                        broker=broker,
                        client_id=client_id,
                        username=username,
                        user_email=user_email
                    ),
                    name=f"email_restored_{client_id}"
                )
            else:
                # ── FAILURE ──
                update_blocked_user_signature(client_id, new_sig)
                b_info = get_blocked_user(client_id)
                fail_reason = b_info.get("reason", "Authentication failed") if b_info else "Authentication failed"
                await logger_auth.warn(f"❌ [CREDENTIALS WATCHER] Updated credentials for {broker}:{client_id} still failed: {fail_reason}")
                
                # Dispatch failure alert email
                asyncio.create_task(
                    send_auth_failed_email(
                        broker=broker,
                        client_id=client_id,
                        username=username,
                        reason=fail_reason,
                        user_email=user_email
                    ),
                    name=f"email_failed_{client_id}"
                )
        except Exception as auth_err:
            err_str = str(auth_err)
            update_blocked_user_signature(client_id, new_sig, reason=err_str)
            await logger_auth.error(f"❌ [CREDENTIALS WATCHER] Exception testing credentials for {broker}:{client_id}: {err_str}")
            asyncio.create_task(
                send_auth_failed_email(
                    broker=broker,
                    client_id=client_id,
                    username=username,
                    reason=err_str,
                    user_email=user_email
                ),
                name=f"email_failed_{client_id}"
            )

    return unblocked_count


async def start_blocked_credentials_watcher(interval_seconds: int = 90) -> None:
    """
    Background worker loop that automatically checks blocked accounts against the source of truth every 90 seconds during market hours.
    Automatically pauses after market hours and on weekends, resuming when the next market session opens (09:15 AM IST).
    """
    await logger_system.info(f"🚀 [CREDENTIALS WATCHER] Background recovery worker started (Interval: {interval_seconds}s).")
    while True:
        try:
            if not is_market_session_active():
                wait_sec = get_seconds_until_next_market_session()
                now_ist = datetime.now(timezone.utc).astimezone(_IST)
                resume_ist = now_ist + timedelta(seconds=wait_sec)
                hours = int(wait_sec // 3600)
                minutes = int((wait_sec % 3600) // 60)
                await logger_system.info(
                    f"⏸️ [CREDENTIALS WATCHER] Market session is closed. Pausing 90s checks for {hours}h {minutes}m. "
                    f"Resuming at {resume_ist.strftime('%Y-%m-%d %H:%M:%S IST')}."
                )
                await asyncio.sleep(wait_sec)
                await logger_system.info("▶️ [CREDENTIALS WATCHER] Market session opened! Resuming 90s checks.")
                continue

            await asyncio.sleep(interval_seconds)
            if not is_market_session_active():
                continue
            await run_blocked_credentials_check_iteration()
        except asyncio.CancelledError:
            logger.info("🛑 [CREDENTIALS WATCHER] Recovery worker stopped.")
            break
        except Exception as e:
            logger.error(f"❌ [CREDENTIALS WATCHER] Unexpected error in watcher loop: {e}", exc_info=True)
            await asyncio.sleep(30.0)
