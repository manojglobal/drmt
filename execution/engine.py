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
            else:
                await logger_exit.warn(f"Unknown broker for exit: {cred.broker}")
                return False
        except Exception as e:
            await logger_exit.error(f"Execution error for {pos.symbol}: {e}")
            return False

async def mass_exit(positions: List[Position], credentials: List[Credential]) -> int:
    """Executes a mass exit for multiple positions concurrently."""
    cred_map = {f"{c.broker}:{c.client_id}": c for c in credentials}
    
    tasks = []
    for pos in positions:
        cred = cred_map.get(f"{pos.broker}:{pos.client_id}")
        if not cred:
            await logger_exit.error(f"No credential found for {pos.broker}:{pos.client_id}")
            continue
            
        tasks.append(execute_exit_order(pos, cred))
        
    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_count = sum(1 for r in results if r is True)
    return success_count
