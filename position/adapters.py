import logging
import json
from typing import List, Dict, Any
from models.schemas import Credential, Position
from credentials.httpclient import proxy_request
from core.engine import core_engine
from position.manager import PositionManager

logger = logging.getLogger(__name__)

async def poll_mstock(cred: Credential) -> List[Position]:
    """MStock Position Polling via DRMT Proxy."""
    # Assuming the API endpoint for MStock positions is a GET request to their Open API
    url = "https://api.mstock.com/positions"
    headers = {"Authorization": f"Bearer {cred.token}"}
    
    # We acquire the global API semaphore via Core Engine internally if needed,
    # but the Core Engine worker pool already limits concurrency.
    
    try:
        resp = await proxy_request("GET", url, str(cred.static_ip), headers=headers)
        data = resp.get("data", [])
        return PositionManager.normalize_positions(data, cred.broker, cred.client_id)
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            raise PermissionError(f"MStock Auth Error: {e}")
        raise

async def poll_flattrade(cred: Credential) -> List[Position]:
    """Flattrade Position Polling via DRMT Proxy."""
    url = "https://api.flattrade.in/PositionBook"
    payload = {"uid": cred.client_id, "actid": cred.client_id}
    # Payload usually requires jData as string, depending on Flattrade docs. We'll send JSON payload.
    
    try:
        # Flattrade typically uses POST and sends jData=...&jKey=...
        resp = await proxy_request("POST", url, str(cred.static_ip), json_data={"jData": payload, "jKey": cred.token})
        
        # Dual-Schema Fallback: stat == "Not_Ok" implies error/empty, no data struct
        if resp.get("stat") == "Not_Ok":
            if "Invalid Session" in resp.get("emsg", ""):
                raise PermissionError("Flattrade Auth Error: Invalid Session")
            return []
            
        return PositionManager.normalize_positions(resp if isinstance(resp, list) else [resp], cred.broker, cred.client_id)
    except Exception as e:
        if "401" in str(e) or "403" in str(e) or isinstance(e, PermissionError):
            raise PermissionError(f"Flattrade Auth Error: {e}")
        raise

async def poll_aliceblue(cred: Credential) -> List[Position]:
    """AliceBlue Position Polling (Direct HTTP)."""
    session = core_engine.http_session
    if not session:
        raise RuntimeError("HTTP Session not initialized")
        
    url = "https://ant.aliceblueonline.com/open-api/od/v1/positions"
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json"
    }
    
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status in (401, 403):
                raise PermissionError("AliceBlue Auth Error: 401/403")
            resp.raise_for_status()
            data = await resp.json()
            
            # Rule: AliceBlue EC920 means no positions, not an error.
            if data.get("stat") == "Not_Ok" and data.get("emsg") == "EC920":
                return []
                
            return PositionManager.normalize_positions(data.get("result", []), cred.broker, cred.client_id)
    except PermissionError:
        raise
    except Exception as e:
        raise RuntimeError(f"AliceBlue Poll Error: {e}")

async def broker_poll(cred: Credential) -> List[Position]:
    """Router for broker polling based on credential."""
    if cred.broker.upper() == "MSTOCK":
        return await poll_mstock(cred)
    elif cred.broker.upper() == "FLATTRADE":
        return await poll_flattrade(cred)
    elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
        return await poll_aliceblue(cred)
    else:
        logger.warning(f"Unknown broker: {cred.broker}")
        return []
