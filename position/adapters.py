import asyncio
import logging
import json
import random
from typing import List, Dict, Any
from models.schemas import Credential, Position
from credentials.httpclient import proxy_request
from core.engine import core_engine
from position.manager import PositionManager
from execution.mstock_instruments import ensure_mstock_instruments_cached
from logger.logger import logger_system

logger = logging.getLogger("DRMT.SCM")

# Tracks last raw payload to avoid redundant log spam when positions are unchanged
_last_raw_payloads: Dict[str, str] = {}

async def poll_mstock(cred: Credential) -> List[Position]:
    """MStock Position Polling via DRMT Proxy."""
    url = "https://api.mstock.trade/openapi/typeb/portfolio/positions"
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "X-PrivateKey": cred.api_key,
        "X-Mirae-Version": "1",
        "User-Agent": "Mozilla/5.0"
    }
    
    try:
        resp = await proxy_request("GET", url, str(cred.static_ip), headers=headers)
        if isinstance(resp, dict):
            if "data" in resp:
                data = resp.get("data") or []
            elif "json" in resp and isinstance(resp["json"], dict):
                data = resp["json"].get("data") or []
            else:
                data = []
        elif isinstance(resp, list):
            data = resp
        else:
            data = []

        if data:
            payload_str = json.dumps(data, default=str)
            if _last_raw_payloads.get(f"mstock:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"mstock:{cred.client_id}"] = payload_str
                log_snippet = payload_str[:10000] + ("... [truncated]" if len(payload_str) > 10000 else "")
                await logger_system.info(f"📥 [mStock Positions Payload] ({cred.client_id}): {log_snippet}")
        else:
            _last_raw_payloads.pop(f"mstock:{cred.client_id}", None)

        positions = PositionManager.normalize_positions(data, cred.broker, cred.client_id)
        
        # Ensure MStock Script Master is cached in Redis (checks Redis first, non-blocking)
        asyncio.create_task(ensure_mstock_instruments_cached(cred))
        
        # Batch fetch live market LTP from mStock quote API
        exchange_tokens = {}
        for p in positions:
            if p.token and p.exchange:
                exchange_tokens.setdefault(p.exchange, []).append(str(p.token))

        if exchange_tokens:
            try:
                quote_resp = await asyncio.wait_for(
                    proxy_request(
                        "GET",
                        "https://api.mstock.trade/openapi/typeb/instruments/quote",
                        str(cred.static_ip),
                        headers={
                            "Authorization": f"Bearer {cred.token}",
                            "X-PrivateKey": cred.api_key,
                            "X-Mirae-Version": "1",
                            "Content-Type": "application/json"
                        },
                        json_data={"mode": "LTP", "exchangeTokens": exchange_tokens}
                    ),
                    timeout=2.0
                )
                data_obj = quote_resp.get("data") if isinstance(quote_resp, dict) else None
                fetched = (data_obj.get("fetched") or []) if isinstance(data_obj, dict) else []
                ltp_map = {str(item.get("symbolToken")): float(item.get("ltp", 0)) for item in fetched}

                for p in positions:
                    if p.token in ltp_map and ltp_map[p.token] > 0:
                        p.ltp = ltp_map[p.token]
                        if p.transaction_type == "BUY":
                            p.pnl = round((p.ltp - p.avg_price) * abs(p.quantity), 2)
                        else:
                            p.pnl = round((p.avg_price - p.ltp) * abs(p.quantity), 2)
            except Exception as e:
                logger.warning(f"Could not fetch live LTP quotes for {cred.client_id}: {e}")

        return positions
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            raise PermissionError(f"MStock Auth Error: {e}")
        raise

async def poll_flattrade(cred: Credential) -> List[Position]:
    """Flattrade Position Polling (Direct or via DRMT Proxy)."""
    url = "https://emergeapi.flattrade.in/getPositonBook"
    payload = {"uid": cred.client_id, "actid": cred.client_id, "jKey": cred.token, "jkey": cred.token}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "source": "WEB",
        "device": "Chrome",
        "Origin": "https://emerge.flattrade.in",
        "Referer": "https://emerge.flattrade.in/"
    }
    
    try:
        if cred.static_ip:
            resp = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.post(url, json=payload, headers=headers) as r:
                if r.status in (401, 403):
                    raise PermissionError(f"Flattrade Auth Error: HTTP {r.status}")
                resp = await r.json(content_type=None)
        
        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}
        
        # EmergeAPI schema: {"status": "S"|"I"|"E", "errMsg": "...", "positionBookArr": [...]}
        if isinstance(resp, dict):
            status = str(resp.get("status", "")).upper()
            stat = str(resp.get("stat", "")).lower()
            emsg = str(resp.get("errMsg", "") or resp.get("emsg", "")).lower()
            if (
                status in ("I", "E")
                or stat == "not_ok"
                or any(x in emsg for x in ("invalid session", "session expired", "invalid key", "not logged in", "user not logged in"))
            ):
                raise PermissionError(
                    f"Flattrade Auth Error: Invalid/Expired session (status={status}, msg={resp.get('errMsg') or resp.get('emsg')})"
                )
            
            if status != "S" and "positionBookArr" not in resp:
                raise PermissionError(
                    f"Flattrade Auth Error: Unexpected response (status={status}, msg={resp.get('errMsg') or resp.get('emsg')})"
                )
            
            raw_list = resp.get("positionBookArr") or []
            if not isinstance(raw_list, list):
                raw_list = [raw_list] if raw_list else []
        elif isinstance(resp, list):
            raw_list = resp
        else:
            raw_list = []

        if raw_list:
            payload_str = json.dumps(raw_list, default=str)
            if _last_raw_payloads.get(f"flattrade:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"flattrade:{cred.client_id}"] = payload_str
                log_snippet = payload_str[:10000] + ("... [truncated]" if len(payload_str) > 10000 else "")
                await logger_system.info(f"📥 [Flattrade Positions Payload] ({cred.client_id}): {log_snippet}")
        else:
            _last_raw_payloads.pop(f"flattrade:{cred.client_id}", None)
            
        return PositionManager.normalize_positions(raw_list, cred.broker, cred.client_id)
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
                raise PermissionError(f"AliceBlue Auth Error: HTTP {resp.status}")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            
            # Check for error or session expiration in 200 OK responses (both stat and status keys)
            stat = str(data.get("stat") or data.get("status") or "").lower()
            emsg = str(data.get("emsg", "") or data.get("message", ""))
            
            if stat == "not_ok":
                # Rule: AliceBlue EC920 means no positions, not an error.
                if "ec920" in emsg.lower():
                    _last_raw_payloads.pop(f"aliceblue:{cred.client_id}", None)
                    return []
                if any(x in emsg.lower() for x in ("login", "session", "token", "unauthorized", "expired")):
                    raise PermissionError(f"AliceBlue Auth Error: {emsg}")
                raise RuntimeError(f"AliceBlue Poll Error: {emsg}")
                
            result = data.get("result", [])
            if isinstance(result, list) and result:
                payload_str = json.dumps(result, default=str)
                if _last_raw_payloads.get(f"aliceblue:{cred.client_id}") != payload_str:
                    _last_raw_payloads[f"aliceblue:{cred.client_id}"] = payload_str
                    log_snippet = payload_str[:10000] + ("... [truncated]" if len(payload_str) > 10000 else "")
                    await logger_system.info(f"📥 [AliceBlue Positions Payload] ({cred.client_id}): {log_snippet}")
            else:
                _last_raw_payloads.pop(f"aliceblue:{cred.client_id}", None)

            if isinstance(result, list):
                return PositionManager.normalize_positions(result, cred.broker, cred.client_id)
            return []
    except PermissionError:
        raise
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            raise PermissionError(f"AliceBlue Auth Error: {e}")
        raise RuntimeError(f"AliceBlue Poll Error: {e}")


async def broker_poll(cred: Credential) -> List[Position]:
    """Router for broker polling based on credential."""
    if cred.broker.upper() == "FAKE":
        return []
    elif cred.broker.upper() == "MSTOCK":
        return await poll_mstock(cred)
    elif cred.broker.upper() == "FLATTRADE":
        return await poll_flattrade(cred)
    elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
        return await poll_aliceblue(cred)
    else:
        logger.warning(f"Unknown broker: {cred.broker}")
        return []
