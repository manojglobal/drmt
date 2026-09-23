import json
import logging
from logger.logger import logger_exit
import uuid
from models.schemas import Credential, Position
from credentials.httpclient import proxy_request

from execution.mstock_instruments import get_mstock_trading_symbol

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
    
    # Look up official exchange trading symbol from Redis Script Master mapping
    trading_symbol = await get_mstock_trading_symbol(pos.symbol, str(pos.token), pos.exchange)
    if not trading_symbol:
        # Fallback formatting if Redis mapping is not yet loaded
        trading_symbol = pos.symbol
        if pos.exchange.upper() == "NSE" and not trading_symbol.endswith("-EQ") and not any(s in trading_symbol for s in ("CE", "PE", "FUT")):
            trading_symbol = f"{pos.symbol}-EQ"
        elif pos.exchange.upper() == "BSE" and not ("-" in trading_symbol) and not any(s in trading_symbol for s in ("CE", "PE", "FUT")):
            trading_symbol = f"{pos.symbol}-A"
    
    payload = {
        "variety": "NORMAL",
        "tradingsymbol": trading_symbol,
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
        "disclosedquantity": "0",
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
        await logger_exit.info(f"📤 [mStock Exit Order Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}")
        resp = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        resp_str = json.dumps(resp, default=str) if isinstance(resp, (dict, list)) else str(resp)
        await logger_exit.info(f"📥 [mStock Exit Order Response] ({cred.client_id}) {pos.symbol}: {resp_str}")
        # Check if response implies success. mStock can return dict or list of dicts.
        resp_obj = resp[0] if isinstance(resp, list) and len(resp) > 0 else resp
        if isinstance(resp_obj, dict):
            status_val = resp_obj.get("status")
            msg_val = str(resp_obj.get("message", "")).upper()
            data_val = resp_obj.get("data")
            if (
                status_val in (True, "true")
                or msg_val == "SUCCESS"
                or (isinstance(data_val, dict) and "orderid" in data_val)
            ):
                return True
        await logger_exit.error(f"MStock exit failed for {pos.symbol}: {resp}")
        return False
    except Exception as e:
        await logger_exit.error(f"MStock exit error for {pos.symbol}: {e}")
        return False
