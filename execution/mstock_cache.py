import asyncio
import logging
from logger.logger import logger_system
import csv
from core.engine import core_engine
from utils.csv_helper import _csv_lock

logger = logging.getLogger(__name__)

class MStockInstrumentCache:
    def __init__(self):
        # Initializes the class with default values and state.
        self.instruments = {}
        
    async def load_instruments(self):
        """
        Caches OpenAPIScripMaster on boot, keeping EQ over non-EQ duplicates.
        This downloads or reads the CSV and parses it.
        """
        url = "https://api.mstock.com/OpenAPIScripMaster.csv"
        session = core_engine.http_session
        if not session:
            await logger_system.warn("HTTP session not ready, skipping MStock instrument cache.")
            return
            
        try:
            await logger_system.info("Downloading MStock OpenAPIScripMaster...")
            async with session.get(url) as resp:
                resp.raise_for_status()
                content = await resp.text()
                
            async with _csv_lock:
                reader = csv.DictReader(content.splitlines())
                for row in reader:
                    symbol = row.get("Symbol") or row.get("TrdSym")
                    token = row.get("Token")
                    series = row.get("Series") or row.get("InstType") or ""
                    
                    if not symbol or not token:
                        continue
                        
                    # Rule: Keep EQ over non-EQ duplicates
                    if symbol in self.instruments:
                        if series.upper() == "EQ":
                            self.instruments[symbol] = token
                    else:
                        self.instruments[symbol] = token
                    
            await logger_system.info(f"Loaded {len(self.instruments)} MStock instruments.")
        except Exception as e:
            await logger_system.error(f"Failed to load MStock OpenAPIScripMaster: {e}")

mstock_cache = MStockInstrumentCache()
