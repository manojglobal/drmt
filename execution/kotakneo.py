import json
import logging
import uuid
import urllib.parse
from typing import Dict, Any

from models.schemas import Credential, Position
from credentials.httpclient import proxy_request
from core.engine import core_engine
from credentials.kotakneo import get_kotak_base_url, get_kotak_headers, login_kotakneo
from logger.logger import logger_exit, sanitize_log_message

logger = logging.getLogger("DRMT.SCM")


async def exit_kotakneo(pos: Position, cred: Credential) -> bool:
    """
    Executes an emergency market order square-off on Kotak Neo Trade API.
    Sends application/x-www-form-urlencoded POST to /quick/order/rule/ms/place.
    """
    base_url = get_kotak_base_url(cred)
    headers = get_kotak_headers(cred)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    if not headers.get("Auth"):
        await logger_exit.error(f"Kotak Neo exit failed for {pos.symbol}: no access_token found for {cred.client_id}")
        return False

    # Ensure token is numeric; resolve from Kotak instrument master if needed
    if not str(pos.token or "").strip().isdigit():
        try:
            from execution.kotak_instruments import get_kotak_exchange_token
            resolved_tok = await get_kotak_exchange_token(pos.symbol, pos.exchange)
            if resolved_tok and str(resolved_tok).isdigit():
                pos.token = str(resolved_tok)
        except Exception:
            pass

    from execution.payload_master import PayloadMaster
    form_data = PayloadMaster.build_kotak_payload(pos)
    order_values = json.loads(form_data.get("jData", "{}"))
    kotak_txn = order_values.get("tt", "")
    qty = order_values.get("qt", "")
    exch_seg = order_values.get("es", "")
    product_code = order_values.get("pc", "")

    url = f"{base_url}/quick/order/rule/ms/place"

    await logger_exit.info(
        f"🚨 [KOTAK ORDER DISPATCH] {pos.symbol} | Side: {kotak_txn} | Qty: {qty} | Seg: {exch_seg} | Prod: {product_code} | Target: {cred.client_id}"
    )

    try:
        resp = await _send_kotak_order(url, form_data, headers, cred.static_ip)
        resp_str = json.dumps(resp, default=str) if isinstance(resp, (dict, list)) else str(resp)
        await logger_exit.info(f"📥 [Kotak Neo Exit Order Response] ({cred.client_id}) {pos.symbol}: {resp_str}")
        success, order_id, err_msg, st_code = _parse_kotak_order_response(resp)

        # In-flight token renewal retry on session invalidation
        if not success and (st_code in (100010, 401, 100008, 403, 1037) or "unauthorized" in err_msg.lower()):
            await logger_exit.warn(f"🔑 [KOTAK TOKEN EXPIRED] {cred.client_id} (stCode={st_code}) — attempting in-flight renewal...")
            try:
                new_token = await login_kotakneo(cred)
                if new_token:
                    headers = get_kotak_headers(cred)
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    resp = await _send_kotak_order(url, form_data, headers, cred.static_ip)
                    retry_str = json.dumps(resp, default=str) if isinstance(resp, (dict, list)) else str(resp)
                    await logger_exit.info(f"📥 [Kotak Neo Exit Order Retry Response] ({cred.client_id}) {pos.symbol}: {retry_str}")
                    success, order_id, err_msg, st_code = _parse_kotak_order_response(resp)
            except Exception as renew_err:
                await logger_exit.error(f"In-flight token renewal failed for {cred.client_id}: {renew_err}")

        if success and order_id:
            await logger_exit.info(f"✅ [KOTAK EXIT CONFIRMED] {pos.symbol} (nOrdNo: {order_id}) for {cred.client_id}")
            return True
        else:
            await logger_exit.error(f"❌ [KOTAK EXIT REJECTED] {pos.symbol} for {cred.client_id}: {err_msg}")
            return False

    except Exception as e:
        await logger_exit.error(f"❌ [KOTAK EXIT EXCEPTION] {pos.symbol} for {cred.client_id}: {e}")
        return False


async def _send_kotak_order(url: str, form_data: str, headers: Dict[str, str], static_ip: str) -> Any:
    """Dispatches order payload via proxy transport or direct HTTP session."""
    if static_ip:
        return await proxy_request("POST", url, str(static_ip), headers=headers, data=form_data)
    else:
        session = core_engine.http_session
        if not session:
            raise RuntimeError("Core Engine HTTP session not initialized")
        async with session.post(url, data=form_data, headers=headers) as r:
            return await r.json(content_type=None)


def _parse_kotak_order_response(resp: Any) -> tuple[bool, str, str, Any]:
    """Parses Kotak Neo order response: returns (success, order_id, error_message, status_code)."""
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except Exception:
            resp = {"raw": resp}

    if not isinstance(resp, dict):
        return False, "", f"Invalid response format: {resp}", None

    stat = str(resp.get("stat") or "").strip()
    st_code = resp.get("stCode")
    n_ord_no = resp.get("nOrdNo")
    err_msg = str(resp.get("errMsg") or resp.get("message") or resp.get("raw") or "")

    if stat == "Ok" and n_ord_no:
        return True, str(n_ord_no), "", st_code

    return False, "", err_msg or f"Kotak rejection (stat={stat}, stCode={st_code})", st_code
