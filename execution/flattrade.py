import logging
import json
import uuid
from logger.logger import logger_exit
from models.schemas import Credential, Position
from core.engine import core_engine
from credentials.httpclient import proxy_request
from credentials.flattrade import login_flattrade

logger = logging.getLogger("DRMT.SCM")

async def exit_flattrade(pos: Position, cred: Credential) -> bool:
    """Flattrade Exit (Direct or Proxy)."""
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
        await logger_exit.info(f"📤 [Flattrade Exit Order Payload] ({cred.client_id}) {pos.symbol}: {json.dumps(payload)}")
        if cred.static_ip:
            data = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass
                
        data_str = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
        await logger_exit.info(f"📥 [Flattrade Exit Order Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                
        data_obj = data[0] if isinstance(data, list) and len(data) > 0 else data
        if isinstance(data_obj, dict):
            status = str(data_obj.get("status", "")).upper()
            stat = str(data_obj.get("stat", "")).lower()
            if status == "S" or stat == "ok" or "norenordno" in data_obj or "orderid" in data_obj:
                return True
                
            emsg = str(data_obj.get("errMsg", "") or data_obj.get("emsg", "") or "").lower()
            if status in ("I", "E") or stat == "not_ok" or any(x in emsg for x in ("invalid session", "session expired", "invalid key", "not logged in", "user not logged in")):
                try:
                    await logger_exit.warn(f"🔑 Flattrade auth expired during exit for {pos.symbol}, attempting automatic re-auth...")
                    new_token = await login_flattrade(cred)
                    if new_token:
                        payload["jKey"] = new_token
                        payload["jkey"] = new_token
                        if cred.static_ip:
                            data = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
                        else:
                            async with session.post(url, json=payload, headers=headers) as resp:
                                data = await resp.json(content_type=None)
                        if isinstance(data, str):
                            try:
                                data = json.loads(data)
                            except Exception:
                                pass
                        data_str = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
                        await logger_exit.info(f"📥 [Flattrade Exit Order Retry Response] ({cred.client_id}) {pos.symbol}: {data_str}")
                        data_obj = data[0] if isinstance(data, list) and len(data) > 0 else data
                        if isinstance(data_obj, dict):
                            status = str(data_obj.get("status", "")).upper()
                            stat = str(data_obj.get("stat", "")).lower()
                            if status == "S" or stat == "ok" or "norenordno" in data_obj or "orderid" in data_obj:
                                return True
                except Exception as retry_err:
                    await logger_exit.error(f"Flattrade exit re-auth retry failed: {retry_err}")

        await logger_exit.error(f"Flattrade exit failed for {pos.symbol} ({cred.client_id}) | Full Response: {data_str}")
        return False
    except Exception as e:
        await logger_exit.error(f"Flattrade exit error for {pos.symbol}: {e}")
        return False
