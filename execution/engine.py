import asyncio
import logging
import time
from typing import List

from core.engine import core_engine
from execution.aliceblue import exit_aliceblue
from execution.flattrade import exit_flattrade
from execution.mstock import exit_mstock
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
