import logging
from logger.logger import logger_exit
import uuid
from models.schemas import Credential, Position
from credentials.httpclient import proxy_request

logger = logging.getLogger(__name__)

async def exit_mstock(pos: Position, cred: Credential) -> bool:
    """MStock Exit via DRMT Proxy."""
    url = "https://api.mstock.com/placeorder"
    
    # MStock specific mappings
    prod_type = pos.product_type.upper()
    if prod_type in ("NFO", "BFO", "NCR"):
        prod_type = "CARRYFORWARD"
        
    tran_type = "SELL" if pos.quantity > 0 else "BUY"
    qty = abs(pos.quantity)
    
    # ordertag truncated to 15 chars
    order_tag = str(uuid.uuid4()).replace("-", "")[:15]
    
    from execution.mstock_cache import mstock_cache
    
    # Resolve token using the EQ-deduped cache if available
    resolved_token = mstock_cache.instruments.get(pos.symbol, pos.token)
    
    payload = {
        "client_id": cred.client_id,
        "symbol": pos.symbol,
        "token": resolved_token,
        "exchange": pos.exchange,
        "transaction_type": tran_type,
        "quantity": qty,
        "product_type": prod_type,
        "order_type": "MARKET",
        "ordertag": order_tag
    }
    
    headers = {"Authorization": f"Bearer {cred.token}"}
    
    try:
        resp = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        # Check if response implies success. Usually 'status': True or 'status': 'Ok'
        if resp.get("status") == True or resp.get("status") == "success":
            return True
        await logger_exit.error(f"MStock exit failed for {pos.symbol}: {resp}")
        return False
    except Exception as e:
        await logger_exit.error(f"MStock exit error for {pos.symbol}: {e}")
        return False
