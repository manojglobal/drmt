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
import logging
from typing import Optional

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

        # Case A: Credentials in Dropbox / static folder MATCH previous failed data (Unchanged)
        if stored_sig and new_sig == stored_sig:
            logger.info(
                f"ℹ️ [CREDENTIALS WATCHER] Credentials for {broker}:{client_id} ({username}) "
                f"match source of truth data (unchanged). Keeping in block list with 0 broker calls."
            )
            continue

        # Case B: Credentials in Dropbox / static folder DO NOT MATCH (Operator modified credentials)
        logger.info(
            f"🔄 [CREDENTIALS WATCHER] Detected updated credentials for {broker}:{client_id} ({username}) "
            f"in source of truth. Attempting single verification..."
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
    Background worker loop that automatically checks blocked accounts against the source of truth every 1–2 minutes.
    """
    await logger_system.info(f"🚀 [CREDENTIALS WATCHER] Background recovery worker started (Interval: {interval_seconds}s).")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await run_blocked_credentials_check_iteration()
        except asyncio.CancelledError:
            logger.info("🛑 [CREDENTIALS WATCHER] Recovery worker stopped.")
            break
        except Exception as e:
            logger.error(f"❌ [CREDENTIALS WATCHER] Unexpected error in watcher loop: {e}", exc_info=True)
