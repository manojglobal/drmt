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

logger = logging.getLogger(__name__)

async def refresh_token_with_backoff(cred: Credential) -> bool:
    """Auto-Refresh on 401/403 with 3 attempts, 2s then 4s backoff (plan spec)."""
    from credentials.mstock import login_mstock
    from credentials.flattrade import login_flattrade
    from credentials.aliceblue import login_aliceblue
    
    delays = [2, 4]  # sleep between attempt 1→2 and 2→3
    for attempt in range(1, 4):  # attempts 1, 2, 3
        try:
            await logger_auth.info(f"Attempting token refresh for {cred.broker}:{cred.client_id} (Attempt {attempt}/3)")
            if cred.broker.upper() == "MSTOCK":
                await login_mstock(cred)
            elif cred.broker.upper() == "FLATTRADE":
                await login_flattrade(cred)
            elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
                await login_aliceblue(cred)
            return True
        except Exception as e:
            await logger_auth.error(f"Refresh failed for {cred.broker}:{cred.client_id} on attempt {attempt}: {e}")
            if attempt <= len(delays):
                await asyncio.sleep(delays[attempt - 1])
    return False

async def polling_loop(user_id: str, ws: web.WebSocketResponse, credentials: list[Credential], engine) -> None:
    """
    Chronos engine while True loop.
    Fetches live data asynchronously without overlapping, diffs against Redis, and pushes to WebSocket.
    """
    await logger_system.info(f"Chronos polling loop started for user {user_id}")
    
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
                
            # 4. Broker Poll (All active credentials)
            new_data_models = []
            for cred in credentials:
                try:
                    new_data_models.extend(await broker_poll(cred))
                except PermissionError:
                    # Auto-Refresh on 401/403 for this specific credential
                    success = await refresh_token_with_backoff(cred)
                    if not success:
                        await logger_system.error(f"Exhausted refresh retries for {cred.broker}:{cred.client_id}. Skipping.")
                except Exception as e:
                    await logger_system.error(f"Poll failed for {cred.broker}:{cred.client_id} - {e}")
            
            new_data = [pos.model_dump() for pos in new_data_models]
            
            # 5. Diff Before Push
            if new_data != last_snapshot:
                # Backpressure guard
                await asyncio.wait_for(
                    ws.send_bytes(json.dumps({"type": "positions", "data": new_data}).encode("utf-8")),
                    timeout=3.0
                )
                await logger_system.info(f"[WebSocket Update] Frontend event triggered for {len(new_data)} active positions")
                await engine.redis_pool.set(redis_key, json.dumps(new_data))
                await logger_system.info(f"[Redis Update] Updated {len(new_data)} positions in cache for user {user_id}")
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
        
    # Cleanup
    if not ws.closed:
        await ws.close()
    engine.remove_user(user_id)
