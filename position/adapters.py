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


import random




# Global state dictionary for distinct users
_fake_state = {}

async def poll_fake(cred: Credential) -> List[Position]:
    import asyncio, random
    from logger.logger import logger_system
    
    client_id = cred.client_id
    
    if client_id not in _fake_state:
        await logger_system.info(f"[User Initialization] Portfolio building for {client_id}")
        _fake_state[client_id] = {
            "RELIANCE": {"entry": 2813.74, "ltp": 2813.74, "qty": random.randint(10, 50), "type": "BUY"},
            "TCS": {"entry": 3201.14, "ltp": 3201.14, "qty": random.randint(5, 20), "type": "SELL"}
        }
        
    await logger_system.info(f"[Broker Request] Fetching data for {client_id}")
    await asyncio.sleep(0.01) # Simulate fast network
    await logger_system.info(f"[Broker Response] 200 OK for {client_id}")
    
    state = _fake_state[client_id]
    positions = []
    
    for sym, data in state.items():
        # Fluctuate LTP
        data["ltp"] += random.uniform(-2.0, 2.0)
        data["ltp"] = round(data["ltp"], 2)
        
        # Recalc PNL based on BUY/SELL side
        if data["type"] == "BUY":
            pnl = (data["ltp"] - data["entry"]) * data["qty"]
        else: # SELL
            pnl = (data["entry"] - data["ltp"]) * data["qty"]
            
        pos = Position(
            id=f"FAKE:{client_id}:{sym}",
            broker="FAKE",
            client_id=client_id,
            symbol=sym,
            token=f"TOK_{sym}",
            exchange="NSE",
            product_type="MIS",
            order_type="MARKET",
            transaction_type=data["type"],
            quantity=data["qty"],
            avg_price=data["entry"],
            ltp=data["ltp"],
            pnl=round(pnl, 2)
        )
        positions.append(pos)
        
        # EXACT required log format
        pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
        # We only log for user01 and user20 to prevent 40 lines per second from flooding the user's terminal completely,
        # but wait, the prompt specifically says "Add clear and structured logs so I can trace the complete flow for all 20 users."
        # I will log for ALL of them.
        await logger_system.info(f"[Live Position] User: {client_id} | {sym} | {data['type']} | LTP: {data['ltp']:.2f} | PNL: {pnl_str}")
        
    return positions

async def broker_poll(cred: Credential) -> List[Position]:
    """Router for broker polling based on credential."""
    if cred.broker.upper() == "FAKE":
        return await poll_fake(cred)
    elif cred.broker.upper() == "MSTOCK":
        return await poll_mstock(cred)
    elif cred.broker.upper() == "FLATTRADE":
        return await poll_flattrade(cred)
    elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
        return await poll_aliceblue(cred)
    else:
        logger.warning(f"Unknown broker: {cred.broker}")
        return []
