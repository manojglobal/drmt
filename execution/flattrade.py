import logging
import json
from typing import Any, Optional, Tuple
from settings import settings
from logger.logger import logger_exit, logger_api
from models.schemas import Credential, Position
from core.engine import core_engine
from credentials.httpclient import proxy_request
from credentials.flattrade import login_flattrade

logger = logging.getLogger("DRMT.SCM")


def _parse_flattrade_response(data: Any) -> Tuple[str, bool, str, bool]:
    """Parses Flattrade order response into (data_str, confirmed, order_id, is_auth_error)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            pass
    data_str = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
    data_obj = data[0] if isinstance(data, list) and len(data) > 0 else data
    if not isinstance(data_obj, dict):
        return data_str, False, "", False

    status = str(data_obj.get("status", "")).upper()
    stat = str(data_obj.get("stat", "")).lower()
    if status == "S" or stat == "ok" or "norenordno" in data_obj or "orderid" in data_obj:
        order_id = str(data_obj.get("norenordno") or data_obj.get("orderid") or "CONFIRMED")
        return data_str, True, order_id, False

    emsg = str(data_obj.get("errMsg", "") or data_obj.get("emsg", "") or "").lower()
    auth_expired = (
        status in ("I", "E")
        or stat == "not_ok"
        or any(x in emsg for x in ("invalid session", "session expired", "invalid key", "not logged in", "user not logged in"))
    )
    return data_str, False, "", auth_expired


async def _post_flattrade_order(
    url: str,
    headers: dict,
    static_ip: str,
    *,
    data: Optional[str] = None,
    json_data: Optional[dict] = None,
) -> Tuple[Any, Optional[PermissionError]]:
    """Dispatches a Flattrade order POST and returns PermissionError separately so in-flight re-auth still runs."""
    if static_ip:
        try:
            kwargs = {"headers": headers}
            if data is not None:
                kwargs["data"] = data
            if json_data is not None:
                kwargs["json_data"] = json_data
            return await proxy_request("POST", url, str(static_ip), **kwargs), None
        except PermissionError as pe:
            return None, pe

    session = core_engine.http_session
    if not session:
        raise RuntimeError("HTTP session not initialized")
    post_kwargs = {"headers": headers}
    if data is not None:
        post_kwargs["data"] = data
    if json_data is not None:
        post_kwargs["json"] = json_data
    async with session.post(url, **post_kwargs) as resp:
        if resp.status in (401, 403):
            err_body = await resp.text()
            return None, PermissionError(f"Flattrade HTTP {resp.status}: {err_body}")
        return await resp.json(content_type=None), None


async def exit_flattrade(pos: Position, cred: Credential) -> bool:
    """Flattrade Exit (SDK or Web flow based on FLATTRADE_FLOW setting)."""
    flow_mode = getattr(settings, "FLATTRADE_FLOW", "WEB").upper()
    is_sdk = (flow_mode == "SDK") and bool(cred.api_key and cred.api_secret)

    if is_sdk:
        return await _exit_flattrade_sdk(pos, cred)
    return await _exit_flattrade_web(pos, cred)


async def _exit_flattrade_sdk(pos: Position, cred: Credential) -> bool:
    """Flattrade Exit via Official Pi Connect SDK flow."""
    url = "https://piconnect.flattrade.in/PiConnectAPI/PlaceOrder"

    from execution.payload_master import PayloadMaster
    payload_data = PayloadMaster.build_flattrade_sdk_payload(pos, cred)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "DRMT/2.0"
    }

    try:
        await logger_api.info(f"📤 [Flattrade SDK Exit Order Payload] ({cred.client_id}) {pos.symbol}: {payload_data}")
        await logger_exit.info(f"📤 Placing Flattrade SDK exit order for {pos.symbol} Qty={abs(pos.quantity)} ({cred.client_id})")
        data, auth_err = await _post_flattrade_order(url, headers, cred.static_ip, data=payload_data)

        if auth_err is None:
            data_str, confirmed, order_id, is_auth_err = _parse_flattrade_response(data)
            await logger_api.info(f"📥 [Flattrade SDK Exit Order Response] ({cred.client_id}) {pos.symbol}: {data_str}")
            if confirmed:
                await logger_api.info(f"✅ [Flattrade SDK Exit Success] ({cred.client_id}) {pos.symbol}: order_id={order_id}")
                return True
        else:
            data_str = str(auth_err)
            is_auth_err = True
            await logger_api.warn(f"🔑 [Flattrade SDK Auth Rejected] ({cred.client_id}) {pos.symbol}: {data_str}")

        if is_auth_err:
            try:
                await logger_exit.warn(f"🔑 Flattrade SDK auth expired during exit for {pos.symbol}, attempting automatic re-auth...")
                new_token = await login_flattrade(cred)
                if new_token:
                    cred.token = new_token
                    retry_payload = PayloadMaster.build_flattrade_sdk_payload(pos, cred)
                    data, retry_auth_err = await _post_flattrade_order(url, headers, cred.static_ip, data=retry_payload)
                    if retry_auth_err is not None:
                        raise retry_auth_err
                    data_str, confirmed, order_id, _ = _parse_flattrade_response(data)
                    await logger_api.info(f"📥 [Flattrade SDK Exit Order Retry Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                    if confirmed:
                        await logger_api.info(f"✅ [Flattrade SDK Exit Retry Success] ({cred.client_id}) {pos.symbol}: order_id={order_id}")
                        return True
            except Exception as retry_err:
                await logger_api.error(f"❌ [Flattrade SDK Exit Re-Auth Failed] ({cred.client_id}): {retry_err}")
                await logger_exit.error(f"Flattrade SDK exit re-auth retry failed: {retry_err}")

        await logger_api.error(f"❌ [Flattrade SDK Exit Failed] ({cred.client_id}) {pos.symbol}: {data_str}")
        await logger_exit.error(f"Flattrade SDK exit failed for {pos.symbol} ({cred.client_id})")
        return False
    except Exception as e:
        await logger_api.error(f"❌ [Flattrade SDK Exit Exception] ({cred.client_id}) {pos.symbol}: {e}")
        await logger_exit.error(f"Flattrade SDK exit error for {pos.symbol}: {e}")
        return False


async def _exit_flattrade_web(pos: Position, cred: Credential) -> bool:
    """Flattrade Exit via Legacy Web Emerge API Flow (Direct or Proxy)."""
    url = "https://emergeapi.flattrade.in/placeOrder"

    from execution.payload_master import PayloadMaster
    payload = PayloadMaster.build_flattrade_payload(pos, cred)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "source": "WEB",
        "device": "Chrome",
        "Origin": "https://emerge.flattrade.in",
        "Referer": "https://emerge.flattrade.in/"
    }

    try:
        await logger_api.info(f"📤 [Flattrade Exit Order Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}")
        await logger_exit.info(f"📤 Placing Flattrade exit order for {pos.symbol} Qty={abs(pos.quantity)} ({cred.client_id})")
        data, auth_err = await _post_flattrade_order(url, headers, cred.static_ip, json_data=payload)

        if auth_err is None:
            data_str, confirmed, order_id, is_auth_err = _parse_flattrade_response(data)
            await logger_api.info(f"📥 [Flattrade Exit Order Response] ({cred.client_id}) {pos.symbol}: {data_str}")
            if confirmed:
                await logger_api.info(f"✅ [Flattrade Exit Success] ({cred.client_id}) {pos.symbol}: order_id={order_id}")
                return True
        else:
            data_str = str(auth_err)
            is_auth_err = True
            await logger_api.warn(f"🔑 [Flattrade Auth Rejected] ({cred.client_id}) {pos.symbol}: {data_str}")

        if is_auth_err:
            try:
                await logger_exit.warn(f"🔑 Flattrade auth expired during exit for {pos.symbol}, attempting automatic re-auth...")
                new_token = await login_flattrade(cred)
                if new_token:
                    payload["jKey"] = new_token
                    payload["jkey"] = new_token
                    data, retry_auth_err = await _post_flattrade_order(url, headers, cred.static_ip, json_data=payload)
                    if retry_auth_err is not None:
                        raise retry_auth_err
                    data_str, confirmed, order_id, _ = _parse_flattrade_response(data)
                    await logger_api.info(f"📥 [Flattrade Exit Order Retry Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                    if confirmed:
                        await logger_api.info(f"✅ [Flattrade Exit Retry Success] ({cred.client_id}) {pos.symbol}: order_id={order_id}")
                        return True
            except Exception as retry_err:
                await logger_api.error(f"❌ [Flattrade Exit Re-Auth Failed] ({cred.client_id}): {retry_err}")
                await logger_exit.error(f"Flattrade exit re-auth retry failed: {retry_err}")

        await logger_api.error(f"❌ [Flattrade Exit Failed] ({cred.client_id}) {pos.symbol}: {data_str}")
        await logger_exit.error(f"Flattrade exit failed for {pos.symbol} ({cred.client_id})")
        return False
    except Exception as e:
        await logger_api.error(f"❌ [Flattrade Exit Exception] ({cred.client_id}) {pos.symbol}: {e}")
        await logger_exit.error(f"Flattrade exit error for {pos.symbol}: {e}")
        return False
