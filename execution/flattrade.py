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
    
    tran_type = "S" if pos.quantity > 0 else "B"
    qty = abs(pos.quantity)
    
    # Flattrade single-letter product codes: C (CNC), M (NRML), I (MIS), B (BO), H (CO)
    prod_type = pos.product_type.upper()
    if prod_type in ("CNC", "DELIVERY", "C"):
        prd = "C"
    elif prod_type in ("NRML", "NORMAL", "CARRYFORWARD", "M"):
        prd = "M"
    elif prod_type in ("MIS", "INTRADAY", "I"):
        prd = "I"
    elif prod_type in ("BO", "B"):
        prd = "B"
    elif prod_type in ("CO", "H"):
        prd = "H"
    else:
        prd = prod_type
        
    app_inst_id = str(uuid.uuid4())
    
    # Flattrade order routing expects BFO for BSE derivatives (CE/PE/FUT)
    exch = pos.exchange
    if str(exch).upper() == "BSE" and any(x in pos.symbol.upper() for x in ("CE", "PE", "FUT")):
        exch = "BFO"
    elif str(exch).upper() == "NSE" and any(x in pos.symbol.upper() for x in ("CE", "PE", "FUT")):
        exch = "NFO"
        
    payload = {
        "uid": cred.client_id,
        "actid": cred.client_id,
        "exch": exch,
        "tsym": pos.symbol,
        "qty": str(qty),
        "prc": "0",
        "prd": prd,
        "trantype": tran_type,
        "prctyp": "MKT",
        "ret": "DAY",
        "remarks": app_inst_id,
        "app_inst_id": app_inst_id,
        "jKey": cred.token,
        "jkey": cred.token
    }
    
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

        await logger_exit.error(f"Flattrade exit failed for {pos.symbol}: {data}")
        return False
    except Exception as e:
        await logger_exit.error(f"Flattrade exit error for {pos.symbol}: {e}")
        return False
