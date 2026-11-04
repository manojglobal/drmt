import asyncio
import json
import logging
from typing import Any, Optional
import uuid

from core.engine import core_engine
from credentials.httpclient import proxy_request
from logger.logger import logger_exit, logger_api
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


def _build_web_payload(pos: Position, cred: Optional[Credential] = None) -> list:
    """Builds order payload for AliceBlue OMK Web Flow (Plato _place_web_order)."""
    from execution.payload_master import PayloadMaster
    return PayloadMaster.build_alice_web_payload(pos, cred)


def _build_sdk_payload(pos: Position, cred: Optional[Credential] = None) -> list:
    """Builds order payload for AliceBlue Open-API / SDK Flow."""
    from execution.payload_master import PayloadMaster
    return PayloadMaster.build_alice_sdk_payload(pos, cred)


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
        payload = _build_sdk_payload(pos, cred)
        flow_label = "SDK"
    else:
        url = ALICE_WEB_ORDER_URL
        headers = dict(ALICE_WEB_HEADERS)
        headers["Authorization"] = f"Bearer {cred.token}"
        payload = _build_web_payload(pos, cred)
        flow_label = "Web"

    try:
        await logger_api.info(f"📤 [AliceBlue {flow_label} Exit Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}")
        await logger_exit.info(f"📤 Placing AliceBlue ({flow_label}) exit order for {pos.symbol} Qty={abs(pos.quantity)} ({cred.client_id})")
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
        await logger_api.info(f"📥 [AliceBlue {flow_label} Exit Response] ({cred.client_id}) {pos.symbol}: {data_str}")

        is_ok, status_msg = _check_order_success(data, flow_mode)
        if is_ok:
            await logger_api.info(f"✅ [AliceBlue Exit Success] ({cred.client_id}) {pos.symbol}: {status_msg}")
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
                        await logger_api.info(f"📥 [AliceBlue {flow_label} Exit Retry Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                        is_ok_retry, retry_msg = _check_order_success(data, flow_mode)
                        if is_ok_retry:
                            await logger_api.info(f"✅ [AliceBlue Exit Retry Success] ({cred.client_id}) {pos.symbol}: {retry_msg}")
                            return True
                        status_msg = retry_msg
                except Exception as retry_err:
                    await logger_api.error(f"❌ [AliceBlue Exit Re-Auth Failed] ({cred.client_id}): {retry_err}")
                    await logger_exit.error(f"AliceBlue {flow_label.lower()} exit re-auth retry failed: {retry_err}")

        await logger_api.error(f"❌ [AliceBlue {flow_label} Exit Failed] ({cred.client_id}) {pos.symbol} | Status: {status_msg} | Full Response: {data_str}")
        await logger_exit.error(f"AliceBlue {flow_label.lower()} exit failed for {pos.symbol} ({cred.client_id}) | Status: {status_msg}")
        return False
    except Exception as e:
        await logger_api.error(f"❌ [AliceBlue {flow_label} Exit Exception] ({cred.client_id}) {pos.symbol}: {e}")
        await logger_exit.error(f"AliceBlue {flow_label.lower()} exit error for {pos.symbol}: {e}")
        return False
