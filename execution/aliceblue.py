import logging
from logger.logger import logger_exit
import uuid
import asyncio
from models.schemas import Credential, Position
from core.engine import core_engine

logger = logging.getLogger(__name__)

async def exit_aliceblue(pos: Position, cred: Credential) -> bool:
    """AliceBlue Exit (Direct)."""
    session = core_engine.http_session
    if not session:
        raise RuntimeError("HTTP session not initialized")
        
    url = "https://ant.aliceblueonline.com/open-api/od/v1/placeOrder"
    
    tran_type = "SELL" if pos.quantity > 0 else "BUY"
    qty = abs(pos.quantity)
    
    payload = {
        "complexty": "REGULAR",
        "discqty": "0",
        "exch": pos.exchange,
        "pCode": pos.product_type,
        "prctyp": "MKT",
        "price": "0",
        "qty": str(qty),
        "ret": "DAY",
        "symbol_id": pos.token,
        "trading_symbol": pos.symbol,
        "transtype": tran_type,
        "trigPrice": "0",
        "orderTag": "DRMT_EXIT",
        # Rule: Hardcoded deviceNumber
        "deviceNumber": "57a9f9b93ffbd16d3e4be86448a7a472"
    }
    
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json"
    }
    
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if data.get("stat") == "Ok" or data.get("status") == "Ok":
                return True
            await logger_exit.error(f"AliceBlue exit failed for {pos.symbol}: {data}")
            return False
    except Exception as e:
        await logger_exit.error(f"AliceBlue exit error for {pos.symbol}: {e}")
        return False
