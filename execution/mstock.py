import logging
from logger.logger import logger_exit
import uuid
from models.schemas import Credential, Position
from credentials.httpclient import proxy_request

logger = logging.getLogger(__name__)

async def exit_mstock(pos: Position, cred: Credential) -> bool:
    """MStock Exit via DRMT Proxy."""
    url = "https://api.mstock.trade/openapi/typeb/orders/regular"
    
    # MStock specific mappings
    prod_type = pos.product_type.upper()
    if prod_type in ("NFO", "BFO", "NCR", "CARRYFORWARD"):
        prod_type = "CARRYFORWARD"
    elif prod_type in ("MIS", "INTRADAY"):
        prod_type = "INTRADAY"
    elif prod_type in ("CNC", "DELIVERY"):
        prod_type = "DELIVERY"
        
    tran_type = "SELL" if pos.quantity > 0 else "BUY"
    qty = abs(pos.quantity)
    
    # ordertag truncated to 15 chars
    order_tag = str(uuid.uuid4()).replace("-", "")[:15]
    resolved_token = pos.token
    
    payload = {
        "variety": "NORMAL",
        "tradingsymbol": pos.symbol,
        "symboltoken": str(resolved_token),
        "exchange": pos.exchange,
        "transactiontype": tran_type,
        "ordertype": "MARKET",
        "quantity": str(qty),
        "producttype": prod_type,
        "price": "0",
        "triggerprice": "0",
        "squareoff": "0",
        "stoploss": "0",
        "trailingStopLoss": "",
        "disclosedquantity": "",
        "duration": "DAY",
        "ordertag": order_tag
    }
    
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "X-PrivateKey": cred.api_key,
        "X-Mirae-Version": "1",
        "Content-Type": "application/json"
    }
    
    try:
        resp = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        # Check if response implies success. Usually 'status': True or 'status': 'true' or 'message': 'SUCCESS'
        if isinstance(resp, dict) and (resp.get("status") in (True, "true") or resp.get("message") == "SUCCESS"):
            return True
        await logger_exit.error(f"MStock exit failed for {pos.symbol}: {resp}")
        return False
    except Exception as e:
        await logger_exit.error(f"MStock exit error for {pos.symbol}: {e}")
        return False
