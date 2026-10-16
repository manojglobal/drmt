import json
import logging
import uuid

from credentials.httpclient import proxy_request
from execution.mstock_instruments import get_mstock_trading_symbol
from logger.logger import logger_exit
from models.schemas import Credential, Position

logger = logging.getLogger(__name__)

async def exit_mstock(pos: Position, cred: Credential) -> bool:
    """MStock Exit via DRMT Proxy."""
    url = "https://api.mstock.trade/openapi/typeb/orders/regular"
    
    # Look up official exchange trading symbol from Redis Script Master mapping
    trading_symbol = await get_mstock_trading_symbol(pos.symbol, str(pos.token), pos.exchange)
    if not trading_symbol:
        # Fallback formatting if Redis mapping is not yet loaded
        trading_symbol = pos.symbol
        if pos.exchange.upper() == "NSE" and not trading_symbol.endswith("-EQ") and not any(s in trading_symbol for s in ("CE", "PE", "FUT")):
            trading_symbol = f"{pos.symbol}-EQ"
        elif pos.exchange.upper() == "BSE" and not ("-" in trading_symbol) and not any(s in trading_symbol for s in ("CE", "PE", "FUT")):
            trading_symbol = f"{pos.symbol}-A"
    
    from execution.payload_master import PayloadMaster
    payload = PayloadMaster.build_mstock_payload(pos, trading_symbol)
    
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
        await logger_exit.error(f"MStock exit failed for {pos.symbol} ({cred.client_id}) | Full Response: {resp_str}")
        return False
    except Exception as e:
        await logger_exit.error(f"MStock exit error for {pos.symbol}: {e}")
        return False
