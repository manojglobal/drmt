import json
import logging
import time
from typing import Dict, Any, Optional

import aiohttp

from settings import settings
from core.engine import core_engine
from logger.logger import logger_api, sanitize_log_message

logger = logging.getLogger("DRMT.SCM")

async def proxy_request(
    method: str,
    url: str,
    source_ip: str,
    headers: Optional[Dict] = None,
    json_data: Optional[Dict] = None,
    data: Optional[Any] = None
) -> Any:
    """Wraps aiohttp to send requests via the DRMT proxy."""
    if not headers:
        headers = {}
        
    # Strip X-Static-IP before forwarding if it exists (the proxy expects source_ip payload or handles it)
    headers.pop('X-Static-IP', None)
    
    payload = {
        "method": method,
        "url": url,
        "source_ip": source_ip,
        "headers": headers
    }
    if json_data is not None:
        payload["json"] = json_data
    if data is not None:
        payload["data"] = data
        
    session = core_engine.http_session
    if not session:
        raise RuntimeError("Core Engine HTTP session not initialized")
        
    t0 = time.monotonic()
    safe_url = sanitize_log_message(url)
    logger.info(f"📤 [PROXY REQ] {method} {safe_url} (source_ip={source_ip})")
    await logger_api.info(f"📤 [PROXY REQ] {method} {safe_url} (source_ip={source_ip})")

    async with session.post(settings.DRMT_PROXY_URL, json=payload) as resp:
        try:
            resp_data = await resp.json()
            resp.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000
            
            # Check if this is wrapped by DRMT proxy
            if isinstance(resp_data, dict) and "status_code" in resp_data:
                target_sc = resp_data.get("status_code", 200)
                broker_body = resp_data.get("json") if resp_data.get("json") is not None else resp_data.get("text", "")
                
                # Parse JSON string if broker returned stringified JSON
                if isinstance(broker_body, str) and (broker_body.startswith("{") or broker_body.startswith("[")):
                    try:
                        broker_body = json.loads(broker_body)
                    except Exception:
                        pass
                
                resp_str = json.dumps(broker_body, default=str) if isinstance(broker_body, (dict, list)) else str(broker_body)
                if len(resp_str) > 2000:
                    resp_str = resp_str[:2000] + "... [truncated]"
                sanitized_resp = sanitize_log_message(resp_str)
                logger.info(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_resp}")
                await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {target_sc} in {elapsed_ms:.1f}ms")
                
                if target_sc in (401, 403):
                    raise PermissionError(f"Broker Auth Error (HTTP {target_sc}): {broker_body}")
                elif target_sc and target_sc >= 400:
                    raise RuntimeError(f"Broker HTTP {target_sc} Error: {broker_body}")
                    
                return broker_body
            else:
                resp_str = json.dumps(resp_data, default=str)
                if len(resp_str) > 2000:
                    resp_str = resp_str[:2000] + "... [truncated]"
                sanitized_resp = sanitize_log_message(resp_str)
                logger.info(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_resp}")
                await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {resp.status} in {elapsed_ms:.1f}ms")
                return resp_data
        except aiohttp.ContentTypeError:
            elapsed_ms = (time.monotonic() - t0) * 1000
            resp_text = await resp.text()
            sanitized_text = sanitize_log_message(resp_text[:500])
            logger.info(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_text}")
            await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {resp.status} (text) in {elapsed_ms:.1f}ms")
            resp.raise_for_status()
            return resp_text
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(f"❌ API Error ({elapsed_ms:.1f}ms): {e}")
            await logger_api.error(f"❌ [PROXY ERR] {method} {safe_url} after {elapsed_ms:.1f}ms: {e}")
            raise
