import asyncio
import json
import logging
from typing import Any
import uuid

from core.engine import core_engine
from credentials.httpclient import proxy_request
from logger.logger import logger_exit
from models.schemas import Credential, Position

logger = logging.getLogger("DRMT.SCM")

# AliceBlue OMK Web Backend Endpoint (Plato Web Flow)
ALICE_WEB_ORDER_URL = "https://antdrn.aliceblueonline.com/omk/od-rest/orders/web/execute"

# Browser simulation headers to emulate AliceBlue ANT Web frontend
ALICE_WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Sec-CH-UA": '"Google Chrome";v="149", "Chromium";v="149"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Origin": "https://ant.aliceblueonline.com",
    "Referer": "https://ant.aliceblueonline.com/",
    "Content-Type": "application/json"
}


def _check_web_order_success(data: Any) -> tuple[bool, str]:
    """Validates AliceBlue Web Flow response, extracting orderNo."""
    if not data:
        return False, "Empty response from broker"

    items = []
    if isinstance(data, dict):
        res = data.get("result")
        if isinstance(res, list):
            items = res
        elif isinstance(res, dict):
            items = [res]
        else:
            items = [data]
    elif isinstance(data, list):
        items = data

    for it in items:
        if isinstance(it, dict):
            stat = str(it.get("stat") or it.get("status") or "").lower()
            if stat.startswith("ec") or stat == "not_ok":
                msg = it.get("message") or it.get("emsg") or f"Broker error {stat}"
                return False, f"{it.get('status', stat)}: {msg}"

            # Web flow returns order ID under orderNo (also checks orderNumber/brokerOrderId)
            order_no = it.get("orderNo") or it.get("orderNumber") or it.get("brokerOrderId") or it.get("omsOrderId")
            if order_no and str(order_no).strip():
                return True, str(order_no)

            if stat == "ok" and "success" in str(it.get("message", "")).lower():
                return True, "SUCCESS"

    return False, "No orderNo in broker response"


async def exit_aliceblue(pos: Position, cred: Credential) -> bool:
    """AliceBlue Exit via OMK Web Flow (Direct or via DRMT Proxy)."""
    url = ALICE_WEB_ORDER_URL

    # Transaction mapping: 'S' for Sell, 'B' for Buy
    trans_type = "S" if pos.quantity > 0 else "B"
    qty = abs(pos.quantity)

    # Product mapping for Web Flow: MIS (Intraday) or CNC (Holding)
    prod_upper = str(pos.product_type).upper()
    prod = "MIS" if prod_upper in ("INTRADAY", "MIS", "I") else "CNC"

    inst_id = str(pos.token)

    # OMK Web Order Payload (Plato Web Schema)
    order_item = {
        "deviceNumber": "57a9f9b93ffbd16d3e4be86448a7a472",
        "exchange": pos.exchange,
        "token": inst_id,
        "tradingSymbol": pos.symbol,
        "ltp": "",
        "qty": str(qty),
        "price": 0,
        "product": prod,
        "priceType": "MKT",
        "orderType": "Regular",
        "transType": trans_type,
        "ret": "DAY",
        "triggerPrice": 0,
        "disclosedQty": "",
        "mktProtection": "",
        "target": "",
        "stopLoss": "",
        "trailingStopLoss": "",
        "orderTag": "DRMT_EXIT"
    }
    payload = [order_item]

    headers = dict(ALICE_WEB_HEADERS)
    headers["Authorization"] = f"Bearer {cred.token}"

    try:
        await logger_exit.info(f"📤 [AliceBlue Web Exit Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}")
        if cred.static_ip:
            data = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json(content_type=None)

        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass

        data_str = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
        await logger_exit.info(f"📥 [AliceBlue Web Exit Response] ({cred.client_id}) {pos.symbol}: {data_str}")

        is_ok, status_msg = _check_web_order_success(data)
        if is_ok:
            return True

        # Check if auth expired, attempt 1 re-auth retry if possible
        data_obj = data[0] if isinstance(data, list) and len(data) > 0 else data
        if isinstance(data_obj, dict):
            emsg = str(data_obj.get("message") or data_obj.get("emsg") or "").lower()
            if any(x in emsg for x in ("session expired", "invalid token", "unauthorized", "login", "401")):
                try:
                    from credentials.aliceblue import login_aliceblue
                    await logger_exit.warn(f"🔑 AliceBlue auth expired during web exit for {pos.symbol}, attempting automatic re-auth...")
                    new_token = await login_aliceblue(cred)
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        if cred.static_ip:
                            data = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
                        else:
                            session = core_engine.http_session
                            if not session:
                                raise RuntimeError("HTTP session not initialized")
                            async with session.post(url, headers=headers, json=payload) as resp:
                                data = await resp.json(content_type=None)
                        if isinstance(data, str):
                            try:
                                data = json.loads(data)
                            except Exception:
                                pass
                        data_str = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
                        await logger_exit.info(f"📥 [AliceBlue Web Exit Retry Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                        is_ok_retry, retry_msg = _check_web_order_success(data)
                        if is_ok_retry:
                            return True
                        status_msg = retry_msg
                except Exception as retry_err:
                    await logger_exit.error(f"AliceBlue web exit re-auth retry failed: {retry_err}")

        await logger_exit.error(f"AliceBlue web exit failed for {pos.symbol}: {status_msg}")
        return False
    except Exception as e:
        await logger_exit.error(f"AliceBlue web exit error for {pos.symbol}: {e}")
        return False
