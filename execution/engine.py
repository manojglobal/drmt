import asyncio
import logging
from logger.logger import logger_exit
from typing import List
from models.schemas import Credential, Position
from core.engine import core_engine
from execution.mstock import exit_mstock
from execution.aliceblue import exit_aliceblue
from execution.flattrade import exit_flattrade

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
            elif cred.broker.upper() == "FAKE":
                from execution.fake_broker import exit_fake
                return await exit_fake(pos, cred)
            else:
                await logger_exit.warn(f"Unknown broker for exit: {cred.broker}")
                return False
        except Exception as e:
            await logger_exit.error(f"Execution error for {pos.symbol}: {e}")
            return False


async def mass_exit(positions: List[Position], credentials: List[Credential]) -> int:
    """Executes a mass exit for multiple positions concurrently through the core engine."""
    cred_map = {f"{c.broker}:{c.client_id}": c for c in credentials}
    
    futures = []
    for pos in positions:
        cred = cred_map.get(f"{pos.broker}:{pos.client_id}")
        if not cred:
            await logger_exit.error(f"No credential found for {pos.broker}:{pos.client_id}")
            continue
            
        future = await core_engine.async_submit(execute_exit_order(pos, cred))
        futures.append(future)
        
    results = await asyncio.gather(*futures, return_exceptions=True)
    success_count = sum(1 for r in results if r is True)
    return success_count
