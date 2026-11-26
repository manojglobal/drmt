import asyncio
import logging
import time
from typing import List, Dict, Union

from core.engine import core_engine
from execution.aliceblue import exit_aliceblue
from execution.flattrade import exit_flattrade
from execution.mstock import exit_mstock
from execution.fyers import exit_fyers
from execution.kotakneo import exit_kotakneo
from execution.axisdirect import exit_axisdirect
from logger.logger import logger_exit
from models.schemas import Credential, Position

logger = logging.getLogger(__name__)

async def execute_exit_order(pos: Position, cred: Credential) -> bool:
    """Dispatches the exit order to the correct broker adapter while respecting the exit semaphore."""
    async with core_engine.acquire_exit_semaphore():
        try:
            if cred.broker.upper() == "MSTOCK":
                return await exit_mstock(pos, cred)
            elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
                return await exit_aliceblue(pos, cred)
            elif cred.broker.upper() == "FLATTRADE":
                return await exit_flattrade(pos, cred)
            elif cred.broker.upper() == "FYERS":
                return await exit_fyers(pos, cred)
            elif cred.broker.upper() in ("KOTAK", "KOTAK NEO", "KOTAKNEO"):
                return await exit_kotakneo(pos, cred)
            elif cred.broker.upper() in ("AXIS", "AXIS DIRECT", "AXISDIRECT"):
                return await exit_axisdirect(pos, cred)
            else:
                await logger_exit.warn(f"Unknown broker for exit: {cred.broker}")
                return False
        except Exception as e:
            await logger_exit.error(f"Execution error for {pos.symbol}: {e}")
            return False


async def mass_exit(positions: List[Position], credentials: List[Credential]) -> tuple[int, List[dict]]:
    """Executes a mass exit for multiple positions concurrently through the core engine."""
    await logger_exit.info(f"🚨 [MASS EXIT START] Initiating square-off for {len(positions)} position(s)...")
    t0 = time.monotonic()
    
    cred_map = {}
    for c in credentials:
        cred_map[f"{c.broker}:{c.client_id}"] = c
        cred_map[f"{c.broker.upper().replace(' ', '')}:{c.client_id}"] = c
        cred_map[c.client_id] = c
    
    tasks = []
    task_positions = []
    missing_results = []

    for pos in positions:
        cred = (
            cred_map.get(f"{pos.broker}:{pos.client_id}")
            or cred_map.get(f"{pos.broker.upper().replace(' ', '')}:{pos.client_id}")
            or cred_map.get(pos.client_id)
        )
        if not cred:
            await logger_exit.error(f"❌ No active credential found for {pos.broker}:{pos.client_id} (symbol={pos.symbol})")
            missing_results.append({
                "broker": pos.broker,
                "symbol": pos.symbol,
                "success": False,
                "message": f"No active credential found for {pos.broker}:{pos.client_id}",
                "position": pos
            })
            continue
            
        task = asyncio.create_task(execute_exit_order(pos, cred))
        tasks.append(task)
        task_positions.append(pos)
        
    raw_results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    
    results = list(missing_results)
    for pos, res in zip(task_positions, raw_results):
        is_success = res is True
        if is_success and pos:
            mark_position_as_exited(pos)
        msg = "Order placed successfully" if is_success else (str(res) if isinstance(res, Exception) else "Broker rejected exit order")
        results.append({
            "broker": pos.broker,
            "symbol": pos.symbol,
            "success": is_success,
            "message": msg,
            "position": pos
        })
        
    success_count = sum(1 for r in results if r["success"] is True)
    elapsed_ms = (time.monotonic() - t0) * 1000
    await logger_exit.info(
        f"🏁 [MASS EXIT COMPLETE] {success_count}/{len(positions)} order(s) placed successfully in {elapsed_ms:.1f}ms"
    )
    return success_count, results


# In-memory cache for recently exited position keys with TTL to prevent race condition echoes
_recently_exited_positions: Dict[str, float] = {}
_EXIT_CACHE_PRUNE_AGE_SEC = 60.0


def _build_exit_key(target: Union[Position, str]) -> str:
    """Builds a canonical uppercase key preserving broker, client_id, symbol, exchange, and product."""
    if isinstance(target, Position):
        parts = (
            str(target.broker or "").strip().upper(),
            str(target.client_id or "").strip().upper(),
            str(target.symbol or "").strip().upper(),
            str(target.exchange or "").strip().upper(),
            str(target.product_type or "").strip().upper(),
        )
        return ":".join(parts)
    return str(target).strip().upper()


def _prune_expired_exits(now: float, max_age_seconds: float = _EXIT_CACHE_PRUNE_AGE_SEC) -> None:
    """Removes expired entries from _recently_exited_positions to bound memory."""
    expired = [k for k, ts in _recently_exited_positions.items() if (now - ts) >= max_age_seconds]
    for k in expired:
        _recently_exited_positions.pop(k, None)


def mark_position_as_exited(position_or_id: Union[Position, str]):
    """Registers a position as successfully exited to prevent stale in-flight scan echoes from restoring it."""
    if not position_or_id:
        return
    key = _build_exit_key(position_or_id)
    if not key:
        return
    now = time.monotonic()
    _prune_expired_exits(now)
    _recently_exited_positions[key] = now


def is_recently_exited(position_or_id: Union[Position, str], ttl_seconds: float = 20.0) -> bool:
    """Checks whether a position matches an exact recently exited key within the TTL window."""
    if not position_or_id:
        return False
    key = _build_exit_key(position_or_id)
    if not key:
        return False
    ts = _recently_exited_positions.get(key)
    return bool(ts and (time.monotonic() - ts) < ttl_seconds)


def get_recently_exited_ids(ttl_seconds: float = 20.0) -> set:
    """Returns the set of active recently exited position IDs."""
    now = time.monotonic()
    expired = [k for k, ts in _recently_exited_positions.items() if (now - ts) >= ttl_seconds]
    for k in expired:
        _recently_exited_positions.pop(k, None)
    return set(_recently_exited_positions.keys())

