import asyncio
import json
import logging
import random
import time
from typing import Optional, Dict, Tuple

from aiohttp import web
from models.schemas import Credential
from position.adapters import broker_poll
from logger.logger import logger_system, logger_auth, logger_monitor
from credentials.mstock import login_mstock
from credentials.flattrade import login_flattrade
from credentials.aliceblue import login_aliceblue
from credentials.fyers import login_fyers
from position.blocklist import add_to_blocklist, remove_from_blocklist, is_blocked
from utils.user_mapping import get_client_username

logger = logging.getLogger(__name__)

# Track poll errors to throttle repeated console logs for persistent failures
_POLL_ERROR_THROTTLE_SEC = 30.0
_last_poll_errors: Dict[str, Tuple[str, float]] = {}  # client_id -> (clean_err, timestamp)

# In-flight concurrency lock per client_id to prevent simultaneous broker login attempts
_AUTH_CLIENT_LOCKS: Dict[str, asyncio.Lock] = {}

def _get_client_auth_lock(client_id: str) -> asyncio.Lock:
    cid_str = str(client_id).strip()
    if cid_str not in _AUTH_CLIENT_LOCKS:
        _AUTH_CLIENT_LOCKS[cid_str] = asyncio.Lock()
    return _AUTH_CLIENT_LOCKS[cid_str]

async def refresh_token_with_backoff(cred: Credential) -> bool:
    """Attempts single-attempt credential login guarded by a per-client lock. If it fails, adds user to blocklist with zero auto-retries."""
    lock = _get_client_auth_lock(cred.client_id)
    if lock.locked():
        await logger_auth.warn(f"⏳ Authentication already in progress for {cred.broker}:{cred.client_id}. Waiting for lock...")

    async with lock:
        username = get_client_username(cred.client_id)
        try:
            token = ""
            if cred.broker.upper() == "MSTOCK":
                token = await login_mstock(cred)
            elif cred.broker.upper() == "FLATTRADE":
                token = await login_flattrade(cred)
            elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
                token = await login_aliceblue(cred)
            elif cred.broker.upper() == "FYERS":
                token = await login_fyers(cred)
            await logger_auth.info(f"✅ Authentication successful for {cred.broker}:{cred.client_id} ({username})")
            remove_from_blocklist(cred.client_id)
            return True
        except Exception as e:
            err_msg = str(e)
            # Transient network / gateway errors (500, 502, 503, 504, Timeout) should NOT trigger blocklist
            is_transient = any(
                code in err_msg
                for code in ("500", "502", "503", "504", "Bad Gateway", "Gateway Timeout", "Timeout", "Connection reset", "Connection refused", "IA500", "Internal Server Error")
            ) or isinstance(e, (asyncio.TimeoutError, ConnectionError))

            if is_transient:
                await logger_auth.warn(f"⚠️  Transient network error authenticating {cred.broker}:{cred.client_id} ({username}): {e} (skipping blocklist)")
                return False

            await logger_auth.error(f"❌ Auth failed for {cred.broker}:{cred.client_id} ({username}): {e}")
            sig = ""
            try:
                from credentials.dynamic_loader import compute_credential_signature, get_dynamic_credentials
                fresh_d = get_dynamic_credentials(cred.client_id)
                sig = compute_credential_signature(fresh_d or cred.dict())
            except Exception:
                pass
            add_to_blocklist(
                client_id=cred.client_id,
                broker=cred.broker,
                username=username,
                reason=err_msg,
                signature=sig
            )
            # Dispatch failure alert email
            try:
                from utils.email import send_auth_failed_email
                from credentials.dynamic_loader import get_client_email
                asyncio.create_task(
                    send_auth_failed_email(
                        broker=cred.broker,
                        client_id=cred.client_id,
                        username=username,
                        reason=err_msg,
                        user_email=get_client_email(cred.client_id)
                    ),
                    name=f"email_auth_failed_{cred.client_id}"
                )
            except Exception:
                pass
            return False

async def polling_loop(user_id: str, ws: web.WebSocketResponse, credentials: list[Credential], engine) -> None:
    try:
        """
        Chronos engine while True loop.
        Fetches live data asynchronously without overlapping, diffs against Redis, and pushes to WebSocket.
        """
        await logger_system.info(f"─── [CHRONOS ENGINE] Polling session started for user {user_id} ({len(credentials)} Accounts) ───")
        
        # 1. Thundering-Herd Jitter
        await asyncio.sleep(random.uniform(0, 1.0))
        
        # 2. Stale Cache Fix: Push immediately from Redis
        redis_key = f"positions:{user_id}"
        last_snapshot_str = await engine.redis_pool.get(redis_key)
        
        if last_snapshot_str:
            try:
                last_snapshot = json.loads(last_snapshot_str)
                await ws.send_bytes(json.dumps({"type": "positions", "data": last_snapshot}).encode("utf-8"))
            except Exception:
                last_snapshot = []
        else:
            last_snapshot = []
            
        engine.last_ping[user_id] = time.monotonic()
            
        while True:
            try:
                # 3. Zombie WebSocket Detection (30s heartbeat)
                last_ping = getattr(engine, 'last_ping', {}).get(user_id, time.monotonic())
                if time.monotonic() - last_ping > 30.0:
                    await logger_system.warn(f"Zombie WebSocket detected for user {user_id} (>30s no ping). Exiting loop.")
                    break
                    
                # 4. Broker Poll (All active credentials concurrently)
                async def _poll_target(c: Credential):
                    if is_blocked(c.client_id):
                        # User is in Block List - skip polling to prevent broker lockout
                        return []

                    # Synchronize fresh token and auth parameters if updated in dynamic cache
                    try:
                        from credentials.dynamic_loader import get_dynamic_credentials
                        fresh = get_dynamic_credentials(c.client_id)
                        if fresh:
                            if fresh.get("token") and fresh["token"] != c.token:
                                c.token = fresh["token"]
                            if fresh.get("api_key") and fresh["api_key"] != c.api_key:
                                c.api_key = fresh["api_key"]
                            if fresh.get("password") and fresh["password"] != c.password:
                                c.password = fresh["password"]
                            if fresh.get("totp_secret") and fresh["totp_secret"] != c.totp_secret:
                                c.totp_secret = fresh["totp_secret"]
                    except Exception:
                        pass

                    try:
                        res = await broker_poll(c)
                        if c.client_id in _last_poll_errors:
                            _last_poll_errors.pop(c.client_id, None)
                            await logger_system.info(f"✅ Connection restored for {c.broker}:{c.client_id}")
                        return res
                    except PermissionError:
                        username = get_client_username(c.client_id)
                        await logger_auth.warn(
                            f"🔑 [CHRONOS] Session token expired/invalid for {c.broker}:{c.client_id} ({username}). "
                            f"Quarantining account in Block list (on-demand auto-refresh is disabled)."
                        )
                        # 1. Immediately quarantine into Block list with signature so future scans skip it
                        sig = ""
                        try:
                            from credentials.dynamic_loader import compute_credential_signature, get_dynamic_credentials
                            fresh_d = get_dynamic_credentials(c.client_id)
                            sig = compute_credential_signature(fresh_d or c.dict())
                        except Exception:
                            pass
                        add_to_blocklist(
                            client_id=c.client_id,
                            broker=c.broker,
                            username=username,
                            reason="Session token expired (HTTP 401/403) — on-demand auto-refresh is disabled.",
                            signature=sig
                        )
                        # 2. Dispatch alert email to operator asynchronously (non-blocking)
                        from utils.email import send_error_email
                        email_body = (
                            f"Account {c.broker}:{c.client_id} ({username}) encountered an expired or invalid session token (HTTP 401/403) during live scanning.\n\n"
                            f"On-demand auto-refresh is disabled during live scans to prevent broker account lockouts.\n"
                            f"The account has been placed in the Block list and live scanning has paused for this account.\n\n"
                            f"To restore authentication, please use the 'Refresh Token' feature on the dashboard or update credentials in the remote CSV."
                        )
                        asyncio.create_task(
                            send_error_email(
                                f"Session Token Expired: {c.broker}:{c.client_id}",
                                email_body
                            ),
                            name=f"email_token_expired_{c.client_id}"
                        )
                        # 3. Return empty list so remaining accounts continue scanning immediately
                        return []
                    except Exception as e:
                        err_str = str(e)
                        is_transient = any(
                            code in err_str
                            for code in ("500", "502", "503", "504", "Bad Gateway", "Gateway Timeout", "Timeout", "Connection reset", "Connection refused", "IA500", "Internal Server Error")
                        ) or isinstance(e, (asyncio.TimeoutError, ConnectionError))

                        now = time.monotonic()
                        prev = _last_poll_errors.get(c.client_id)
                        # Throttle consecutive identical errors for 30s per account
                        if not prev or prev[0] != err_str or (now - prev[1]) >= _POLL_ERROR_THROTTLE_SEC:
                            _last_poll_errors[c.client_id] = (err_str, now)
                            if is_transient:
                                await logger_system.warn(f"⚠️  Poll transient error for {c.broker}:{c.client_id} - {e}")
                            else:
                                await logger_system.error(f"❌ Poll failed for {c.broker}:{c.client_id} - {e}")
                        return []

                t_poll_start = time.monotonic()
                poll_batch = await asyncio.gather(*[_poll_target(c) for c in credentials], return_exceptions=True)
                poll_duration_ms = (time.monotonic() - t_poll_start) * 1000
                new_data_models = []
                for res in poll_batch:
                    if isinstance(res, list):
                        new_data_models.extend(res)
                
                target_count = len(new_data_models) if new_data_models else len(credentials)
                await logger_system.info(f"─── [POSITION SCAN] Scanned {target_count} Targets in {poll_duration_ms:.1f}ms ───")
                if new_data_models:
                    for pos in new_data_models:
                        pnl_pct = f"{(pos.pnl / (pos.avg_price * abs(pos.quantity))) * 100:+.2f}%" if (pos.avg_price and pos.quantity) else "+0.00%"
                        pnl_str = f"+₹{pos.pnl:.2f}" if pos.pnl >= 0 else f"-₹{abs(pos.pnl):.2f}"
                        status = "Standby" if pos.quantity == 0 else "Holding"
                        pos_line = f"  ⏳ [{pos.symbol}] LTP: ₹{pos.ltp:.2f} | Avg: ₹{pos.avg_price:.2f} | Qty: {pos.quantity} | PnL: {pnl_str} ({pnl_pct}) ➔ {status}"
                        await logger_system.info(pos_line)
                        await logger_auth.info(pos_line)
                else:
                    for cred in credentials:
                        if not cred.client_id or not cred.client_id.strip() or cred.broker.upper() == "FAKE":
                            continue
                        username = get_client_username(cred.client_id)
                        user_label = f" ({username})" if username and username != cred.client_id else ""
                        if is_blocked(cred.client_id):
                            await logger_system.info(f"  ⛔ [{cred.broker}:{cred.client_id}{user_label}] Blocked (Check Block list in Terminal)")
                        else:
                            standby_line = f"  ⏳ [{cred.broker}:{cred.client_id}{user_label}] LTP: ₹0.00 | Positions: 0 | PnL: ₹0.00 ➔ Standby"
                            await logger_system.info(standby_line)
                            await logger_auth.info(standby_line)
                
                new_data = [pos.model_dump() for pos in new_data_models]
                
                # 5. Diff Before Push
                if new_data != last_snapshot:
                    # Backpressure guard
                    await asyncio.wait_for(
                        ws.send_bytes(json.dumps({"type": "positions", "data": new_data}).encode("utf-8")),
                        timeout=3.0
                    )
                    await engine.redis_pool.set(redis_key, json.dumps(new_data))
                    await logger_system.info(f"⚡ [POSITION PUSH] Pushed {len(new_data)} position(s) to WebSocket and synced Redis cache")
                    await logger_monitor.info(f"⚡ [POSITION PUSH] user={user_id} | Pushed {len(new_data)} position(s) to WebSocket: {json.dumps(new_data, default=str)}")
                    last_snapshot = new_data
                    
            except asyncio.TimeoutError:
                await logger_system.warn(f"WebSocket send timeout for {user_id}. Client slow/dead.")
                break
                
            except ConnectionError:
                await logger_system.info(f"WebSocket disconnected for {user_id}.")
                break
                
            except Exception as e:
                # Glass Jaw: never crash the loop
                await logger_system.error(f"Unexpected error in polling loop for {user_id}: {e}")
                
            # 6. Non-Overlapping Sleep
            await asyncio.sleep(1.0)
            
    finally:
        await logger_system.info(f"─── [CHRONOS ENGINE] Polling session terminated for user {user_id} ───")
        # Cleanup
        if not ws.closed:
            await ws.close()
        engine.remove_user(user_id)
