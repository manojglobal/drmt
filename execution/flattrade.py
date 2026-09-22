import logging
import json
import uuid
from logger.logger import logger_exit
from models.schemas import Credential, Position
from core.engine import core_engine
from credentials.httpclient import proxy_request

logger = logging.getLogger("DRMT.SCM")

async def exit_flattrade(pos: Position, cred: Credential) -> bool:
    """Flattrade Exit (Direct or Proxy)."""
    url = "https://piconnect.flattrade.in/PiConnectAPI/PlaceOrder"
    
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
    
    payload = {
        "uid": cred.client_id,
        "actid": cred.client_id,
        "exch": pos.exchange,
        "tsym": pos.symbol,
        "qty": str(qty),
        "prc": "0",
        "prd": prd,
        "trantype": tran_type,
        "prctyp": "MKT",
        "ret": "DAY",
        "remarks": app_inst_id,
        "app_inst_id": app_inst_id
    }
    
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    body = f"jData={json.dumps(payload)}&jKey={cred.token}"
    
    try:
        if cred.static_ip:
            data = await proxy_request("POST", url, str(cred.static_ip), headers=headers, data=body)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.post(url, data=body, headers=headers) as resp:
                data = await resp.json(content_type=None)
                
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass
                
        if isinstance(data, dict) and data.get("stat") == "Ok":
            return True
        await logger_exit.error(f"Flattrade exit failed for {pos.symbol}: {data}")
        return False
    except Exception as e:
        await logger_exit.error(f"Flattrade exit error for {pos.symbol}: {e}")
        return False
