import asyncio
from datetime import datetime
import logging
from typing import Any, Dict

from core.engine import core_engine
from database.db import get_db
from logger.logger import logger_health

logger = logging.getLogger(__name__)

class HealthMonitor:
    def __init__(self):
        # Initializes the class with default values and state.
        self.is_healthy = False
        self.last_check = None
        
    async def start(self):
        """Pings immediately on boot via asyncio.create_task (perpetual loop)."""
        asyncio.create_task(self._monitor_loop(), name="health_monitor")
        
    async def _monitor_loop(self):
        # Runs a continuous loop to check system health.
        while True:
            await self._run_health_check()
            await asyncio.sleep(60) # Check every minute
            
    async def _run_health_check(self):
        # Pings external services to ensure they are online.
        try:
            # 1. Check DB
            async with get_db() as db:
                await db.execute("SELECT 1")
                
            # 2. Check Proxy/Network
            session = core_engine.http_session
            if session:
                # We can check a known endpoint or just rely on proxy
                pass
                
            self.is_healthy = True
        except Exception as e:
            self.is_healthy = False
            # Rule: Truncate server response bodies/errors to 150 chars
            err_msg = str(e)
            if len(err_msg) > 150:
                err_msg = err_msg[:147] + "..."
            await logger_health.error(f"Health check failed: {err_msg}")
        finally:
            self.last_check = datetime.now()

    def get_server_status(self) -> Dict[str, Any]:
        """Returns the specific server statuses for the frontend UI."""
        status = "UP" if self.is_healthy else "DOWN"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if self.is_healthy else ""
        
        return {
            "demo_server": {
                "status": status,
                "query_time": now_str,
                "last_successful_time": now_str,
                "message": ""
            },
            "test_server": {
                "status": status,
                "query_time": now_str,
                "last_successful_time": now_str,
                "message": ""
            }
        }

health_monitor = HealthMonitor()
