import asyncio
import json
import logging
import random
import time
from typing import Optional

from aiohttp import web
from models.schemas import Credential
from position.adapters import broker_poll
from logger.logger import logger_system, logger_auth
from credentials.mstock import login_mstock
from credentials.flattrade import login_flattrade
from credentials.aliceblue import login_aliceblue
from position.blocklist import add_to_blocklist, remove_from_blocklist, is_blocked
from utils.user_mapping import get_client_username

logger = logging.getLogger(__name__)

async def refresh_token_with_backoff(cred: Credential) -> bool:
    """Attempts single-attempt credential login. If it fails, adds user to blocklist with zero auto-retries."""
    username = get_client_username(cred.client_id)
    try:
        token = ""
        if cred.broker.upper() == "MSTOCK":
            token = await login_mstock(cred)
        elif cred.broker.upper() == "FLATTRADE":
            token = await login_flattrade(cred)
        elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
            token = await login_aliceblue(cred)
        masked = f"{token[:10]}...{token[-10:]}" if token and len(token) > 20 else str(token)
        await logger_auth.info(f"🔑 Loaded dynamic auth token: {masked}")
        remove_from_blocklist(cred.client_id)
        return True
    except Exception as e:
        await logger_auth.error(f"Auth failed for {cred.broker}:{cred.client_id} ({username}): {e}")
        add_to_blocklist(
            client_id=cred.client_id,
            broker=cred.broker,
            username=username,
            reason=str(e)
        )
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

                    try:
                        return await broker_poll(c)
                    except PermissionError:
                        await logger_auth.warn(f"🔑 Auth token expired for {c.broker}:{c.client_id} ➔ Refreshing")
                        success = await refresh_token_with_backoff(c)
                        if success:
                            try:
                                return await broker_poll(c)
                            except Exception as e:
                                await logger_system.error(f"❌ Post-refresh poll failed for {c.broker}:{c.client_id} - {e}")
                                return []
                        else:
                            await logger_system.error(f"❌ Authentication failed for {c.broker}:{c.client_id}. Placed in Block List.")
                            return []
                    except Exception as e:
                        await logger_system.error(f"❌ Poll failed for {c.broker}:{c.client_id} - {e}")
                        return []

                poll_batch = await asyncio.gather(*[_poll_target(c) for c in credentials], return_exceptions=True)
                new_data_models = []
                for res in poll_batch:
                    if isinstance(res, list):
                        new_data_models.extend(res)
                
                target_count = len(new_data_models) if new_data_models else len(credentials)
                await logger_system.info(f"─── [POSITION SCAN] Scanning {target_count} Targets ───")
                if new_data_models:
                    for pos in new_data_models:
                        pnl_pct = f"{(pos.pnl / (pos.avg_price * abs(pos.quantity))) * 100:+.2f}%" if (pos.avg_price and pos.quantity) else "+0.00%"
                        pnl_str = f"+₹{pos.pnl:.2f}" if pos.pnl >= 0 else f"-₹{abs(pos.pnl):.2f}"
                        status = "Standby" if pos.quantity == 0 else "Holding"
                        await logger_system.info(
                            f"  ⏳ [{pos.symbol}] LTP: ₹{pos.ltp:.2f} | Avg: ₹{pos.avg_price:.2f} | Qty: {pos.quantity} | PnL: {pnl_str} ({pnl_pct}) ➔ {status}"
                        )
                else:
                    for cred in credentials:
                        if not cred.client_id or not cred.client_id.strip() or cred.broker.upper() == "FAKE":
                            continue
                        username = get_client_username(cred.client_id)
                        user_label = f" ({username})" if username and username != cred.client_id else ""
                        if is_blocked(cred.client_id):
                            await logger_system.info(f"  ⛔ [{cred.broker}:{cred.client_id}{user_label}] Blocked (Check Block list in Terminal)")
                        else:
                            await logger_system.info(f"  ⏳ [{cred.broker}:{cred.client_id}{user_label}] LTP: ₹0.00 | Positions: 0 | PnL: ₹0.00 ➔ Standby")
                
                new_data = [pos.model_dump() for pos in new_data_models]
                
                # 5. Diff Before Push
                if new_data != last_snapshot:
                    # Backpressure guard
                    await asyncio.wait_for(
                        ws.send_bytes(json.dumps({"type": "positions", "data": new_data}).encode("utf-8")),
                        timeout=3.0
                    )
                    await engine.redis_pool.set(redis_key, json.dumps(new_data))
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
