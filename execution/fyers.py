import json
import logging

from core.engine import core_engine
from credentials.fyers import get_fyers_auth_header
from credentials.httpclient import proxy_request
from logger.logger import logger_exit
from models.schemas import Credential, Position

logger = logging.getLogger("DRMT.SCM")

FYERS_ORDERS_SYNC_URL = "https://api-t1.fyers.in/api/v3/orders/sync"


async def exit_fyers(pos: Position, cred: Credential) -> bool:
    """
    Fyers Position Exit via Market Order sync endpoint.
    Places an offsetting Market order (-1 for SELL, 1 for BUY).
    """
    auth_header = get_fyers_auth_header(cred)
    if not auth_header:
        await logger_exit.error(
            f"Fyers exit failed for {pos.symbol}: no access_token found for {cred.client_id}"
        )
        return False

    from execution.payload_master import PayloadMaster
    payload = PayloadMaster.build_fyers_payload(pos)

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Fyers Adapter)",
    }

    try:
        await logger_exit.info(
            f"📤 [Fyers Exit Order Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}"
        )

        if cred.static_ip:
            data = await proxy_request(
                "POST", FYERS_ORDERS_SYNC_URL, str(cred.static_ip), headers=headers, json_data=payload
            )
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.post(FYERS_ORDERS_SYNC_URL, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)

        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass

        data_str = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
        await logger_exit.info(f"📥 [Fyers Exit Order Response] ({cred.client_id}) {pos.symbol}: {data_str}")

        data_obj = data[0] if isinstance(data, list) and len(data) > 0 else data
        if isinstance(data_obj, dict):
            s_val = str(data_obj.get("s", "")).lower()
            code_val = data_obj.get("code")
            order_id = data_obj.get("id") or data_obj.get("orderNum")

            if s_val == "ok" or code_val == 200 or order_id:
                return True

            msg = data_obj.get("message") or data_obj.get("errMsg") or "Unknown error"
            await logger_exit.error(f"Fyers exit order rejected for {pos.symbol} ({cred.client_id}): {msg} (code={code_val}) | Full Response: {data_str}")
            return False

        await logger_exit.error(f"Fyers exit failed for {pos.symbol} ({cred.client_id}): unexpected response format | Full Response: {data_str}")
        return False

    except Exception as e:
        await logger_exit.error(f"Fyers exit error for {pos.symbol}: {e}")
        return False
