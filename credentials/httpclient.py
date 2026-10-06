import asyncio
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
    json_data: Optional[Any] = None,
    data: Optional[Any] = None
) -> Any:
    """Wraps aiohttp to send requests via the DRMT proxy.

    If the proxy returns HTTP 500 (usually caused by an unavailable
    source_ip on the proxy), retries once without source_ip.
    """
    return await _proxy_request_inner(method, url, source_ip, headers, json_data, data)


async def _proxy_request_inner(
    method: str,
    url: str,
    source_ip: str,
    headers: Optional[Dict] = None,
    json_data: Optional[Any] = None,
    data: Optional[Any] = None,
    _is_retry: bool = False,
) -> Any:
    if not headers:
        headers = {}

    # Strip X-Static-IP before forwarding if it exists (the proxy expects source_ip payload or handles it)
    headers.pop('X-Static-IP', None)

    payload: Dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": headers
    }
    if source_ip and not _is_retry:
        payload["source_ip"] = source_ip
    if json_data is not None:
        payload["json"] = json_data
    if data is not None:
        payload["data"] = data

    session = core_engine.http_session
    if not session:
        raise RuntimeError("Core Engine HTTP session not initialized")

    t0 = time.monotonic()
    safe_url = sanitize_log_message(url)
    retry_tag = " [no-ip retry]" if _is_retry else ""
    logger.debug(f"📤 [PROXY REQ] {method} {safe_url} (source_ip={source_ip}){retry_tag}")
    await logger_api.info(f"📤 [PROXY REQ] {method} {safe_url} (source_ip={source_ip}){retry_tag}")

    async with session.post(settings.DRMT_PROXY_URL, json=payload) as resp:
        try:
            resp_data = await resp.json()

            # HTTP 500 from proxy usually means source_ip is unavailable — retry without it
            if resp.status == 500 and not _is_retry:
                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.warning(f"⚠️  [PROXY 500] {method} {safe_url} ({elapsed_ms:.1f}ms) — retrying without source_ip={source_ip}")
                return await _proxy_request_inner(method, url, "", headers, json_data, data, _is_retry=True)

            resp.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000

            # Check if this is wrapped by DRMT proxy
            if isinstance(resp_data, dict) and "status_code" in resp_data:
                target_sc = resp_data.get("status_code", 200)
                broker_body = resp_data.get("json") if resp_data.get("json") is not None else resp_data.get("text", "")

                # 500/502/503/504 wrapped response means source_ip route failed or broker ALB had a temporary glitch — retry once
                if target_sc in (500, 502, 503, 504) and not _is_retry:
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    tag = f"without source_ip={source_ip}" if source_ip else "after 300ms backoff"
                    logger.warning(f"⚠️  [PROXY {target_sc}] {method} {safe_url} ({elapsed_ms:.1f}ms) — retrying {tag}")
                    await asyncio.sleep(0.3)
                    return await _proxy_request_inner(method, url, "", headers, json_data, data, _is_retry=True)

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
                logger.debug(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_resp}")
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
                logger.debug(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_resp}")
                await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {resp.status} in {elapsed_ms:.1f}ms")
                return resp_data
        except aiohttp.ContentTypeError:
            elapsed_ms = (time.monotonic() - t0) * 1000
            resp_text = await resp.text()

            # HTTP 500 text response — retry without source_ip
            if resp.status == 500 and not _is_retry:
                logger.warning(f"⚠️  [PROXY 500] {method} {safe_url} ({elapsed_ms:.1f}ms) — retrying without source_ip={source_ip}")
                return await _proxy_request_inner(method, url, "", headers, json_data, data, _is_retry=True)

            sanitized_text = sanitize_log_message(resp_text[:500])
            logger.debug(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_text}")
            await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {resp.status} (text) in {elapsed_ms:.1f}ms")
            resp.raise_for_status()
            return resp_text
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.debug(f"❌ API Error ({elapsed_ms:.1f}ms): {e}")
            await logger_api.error(f"❌ [PROXY ERR] {method} {safe_url} after {elapsed_ms:.1f}ms: {e}")
            raise

