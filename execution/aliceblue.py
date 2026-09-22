import logging
from logger.logger import logger_exit
import uuid
import asyncio
from models.schemas import Credential, Position
from core.engine import core_engine

logger = logging.getLogger("DRMT.SCM")

async def exit_aliceblue(pos: Position, cred: Credential) -> bool:
    """AliceBlue Exit (Direct)."""
    session = core_engine.http_session
    if not session:
        raise RuntimeError("HTTP session not initialized")
        
    url = "https://ant.aliceblueonline.com/open-api/od/v1/orders/placeorder"
    
    tran_type = "SELL" if pos.quantity > 0 else "BUY"
    qty = abs(pos.quantity)
    
    prod = pos.product_type.upper()
    if prod in ("INTRADAY", "MIS"):
        prod = "MIS"
    elif prod in ("DELIVERY", "CNC"):
        prod = "CNC"
    elif prod in ("CARRYFORWARD", "NRML", "NORMAL"):
        prod = "NRML"
        
    inst_id = str(pos.token)
    
    order_item = {
        "exchange": pos.exchange,
        "instrumentId": inst_id,
        "transactionType": tran_type,
        "quantity": qty,
        "product": prod,
        "orderComplexity": "REGULAR",
        "orderType": "MARKET",
        "validity": "DAY",
        "price": 0.0,
        "orderTag": "DRMT_EXIT",
        "deviceId": "57a9f9b93ffbd16d3e4be86448a7a472"
    }
    payload = [order_item]
    
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json"
    }
    
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json(content_type=None)
            
            is_ok = False
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
                if isinstance(item, dict) and (
                    str(item.get("stat") or item.get("status") or "").lower() == "ok"
                    or "orderNumber" in item
                    or "omsOrderId" in item
                    or "brokerOrderId" in item
                ):
                    is_ok = True
            elif isinstance(data, dict):
                if (
                    str(data.get("stat") or data.get("status") or "").lower() == "ok"
                    or "orderNumber" in data
                    or "brokerOrderId" in str(data)
                ):
                    is_ok = True
                    
            if is_ok:
                return True
            await logger_exit.error(f"AliceBlue exit failed for {pos.symbol}: {data}")
            return False
    except Exception as e:
        await logger_exit.error(f"AliceBlue exit error for {pos.symbol}: {e}")
        return False
