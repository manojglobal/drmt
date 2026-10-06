import asyncio
import json
import logging
from typing import Any
import uuid

from core.engine import core_engine
from credentials.httpclient import proxy_request
from logger.logger import logger_exit
from models.schemas import Credential, Position
from settings import settings

logger = logging.getLogger("DRMT.SCM")

# AliceBlue Endpoints
ALICE_WEB_ORDER_URL = "https://antdrn.aliceblueonline.com/omk/od-rest/orders/web/execute"
ALICE_SDK_ORDER_URL = "https://ant.aliceblueonline.com/open-api/od/v1/orders/placeorder"

# Browser simulation headers to emulate AliceBlue ANT Web frontend (Plato Web Flow)
ALICE_WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Sec-CH-UA": '"Google Chrome";v="149", "Chromium";v="149"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Origin": "https://ant.aliceblueonline.com",
    "Referer": "https://ant.aliceblueonline.com/",
    "Content-Type": "application/json"
}


def _build_web_payload(pos: Position) -> list:
    """Builds order payload for AliceBlue OMK Web Flow (Plato _place_web_order)."""
    trans_type = "S" if pos.quantity > 0 else "B"
    qty = abs(pos.quantity)
    prod_upper = str(pos.product_type).upper()
    prod = "MIS" if prod_upper in ("INTRADAY", "MIS", "I") else "CNC"
    inst_id = str(pos.token)

    return [{
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
    }]


def _build_sdk_payload(pos: Position) -> list:
    """Builds order payload for AliceBlue Open-API / SDK Flow."""
    trans_type = "SELL" if pos.quantity > 0 else "BUY"
    qty = abs(pos.quantity)
    prod_upper = str(pos.product_type).upper()

    if prod_upper in ("INTRADAY", "MIS", "I"):
        prod = "INTRADAY"
    elif prod_upper in ("DELIVERY", "CNC", "LONGTERM", "C"):
        prod = "LONGTERM"
    elif prod_upper in ("MTF",):
        prod = "MTF"
    elif prod_upper in ("CARRYFORWARD", "NRML", "NORMAL", "M"):
        prod = "LONGTERM" if str(pos.exchange).upper() in ("NSE", "BSE") else "NORMAL"
    else:
        prod = "INTRADAY"

    # Marketable Limit price to guarantee immediate execution on Open-API SDK
    base_price = pos.ltp if (pos.ltp and pos.ltp > 0) else (pos.avg_price if (pos.avg_price and pos.avg_price > 0) else 0.0)
    if base_price > 0:
        raw_price = max(0.05, base_price * 0.99) if trans_type == "SELL" else (base_price * 1.01)
        limit_price = round(round(raw_price / 0.05) * 0.05, 2)
    else:
        limit_price = 0.05

    return [{
        "exchange": pos.exchange,
        "instrumentId": str(pos.token),
        "transactionType": trans_type,
        "quantity": qty,
        "product": prod,
        "orderComplexity": "REGULAR",
        "orderType": "LIMIT",
        "validity": "DAY",
        "price": limit_price,
        "disclosedQuantity": 0,
        "orderTag": "DRMT_EXIT",
        "deviceId": "57a9f9b93ffbd16d3e4be86448a7a472"
    }]


def _check_order_success(data: Any, flow_mode: str = "WEB") -> tuple[bool, str]:
    """Validates AliceBlue response across Web (orderNo) and SDK (brokerOrderId/orderNumber) flows."""
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

            # Check order ID (Web flow returns orderNo; SDK flow returns brokerOrderId/orderNumber)
            order_id = it.get("orderNo") or it.get("orderNumber") or it.get("brokerOrderId") or it.get("omsOrderId")
            if order_id and str(order_id).strip():
                return True, str(order_id)

            if stat == "ok" and "success" in str(it.get("message", "")).lower():
                return True, "SUCCESS"

    return False, "No valid order confirmation in broker response"


async def exit_aliceblue(pos: Position, cred: Credential) -> bool:
    """AliceBlue Exit via Web Flow or SDK Flow (Direct or via DRMT Proxy)."""
    flow_mode = getattr(settings, "ALICE_BLUE_FLOW", "WEB").upper()

    if flow_mode == "SDK":
        url = ALICE_SDK_ORDER_URL
        headers = {
            "Authorization": f"Bearer {cred.token}",
            "Content-Type": "application/json"
        }
        payload = _build_sdk_payload(pos)
        flow_label = "SDK"
    else:
        url = ALICE_WEB_ORDER_URL
        headers = dict(ALICE_WEB_HEADERS)
        headers["Authorization"] = f"Bearer {cred.token}"
        payload = _build_web_payload(pos)
        flow_label = "Web"

    try:
        await logger_exit.info(f"📤 [AliceBlue {flow_label} Exit Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}")
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
        await logger_exit.info(f"📥 [AliceBlue {flow_label} Exit Response] ({cred.client_id}) {pos.symbol}: {data_str}")

        is_ok, status_msg = _check_order_success(data, flow_mode)
        if is_ok:
            return True

        # Check if auth expired, attempt 1 re-auth retry if possible
        data_obj = data[0] if isinstance(data, list) and len(data) > 0 else data
        if isinstance(data_obj, dict):
            emsg = str(data_obj.get("message") or data_obj.get("emsg") or "").lower()
            if any(x in emsg for x in ("session expired", "invalid token", "unauthorized", "login", "401")):
                try:
                    from credentials.aliceblue import login_aliceblue
                    await logger_exit.warn(f"🔑 AliceBlue auth expired during {flow_label.lower()} exit for {pos.symbol}, attempting automatic re-auth...")
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
                        await logger_exit.info(f"📥 [AliceBlue {flow_label} Exit Retry Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                        is_ok_retry, retry_msg = _check_order_success(data, flow_mode)
                        if is_ok_retry:
                            return True
                        status_msg = retry_msg
                except Exception as retry_err:
                    await logger_exit.error(f"AliceBlue {flow_label.lower()} exit re-auth retry failed: {retry_err}")

        await logger_exit.error(f"AliceBlue {flow_label.lower()} exit failed for {pos.symbol}: {status_msg}")
        return False
    except Exception as e:
        await logger_exit.error(f"AliceBlue {flow_label.lower()} exit error for {pos.symbol}: {e}")
        return False
