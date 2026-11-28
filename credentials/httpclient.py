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

    Retries once after brief backoff on transient proxy/upstream 5xx errors
    while strictly preserving any configured static source_ip.
    """
    return await _proxy_request_inner(method, url, source_ip, headers, json_data, data)


async def _retry_proxy_request(
    status_label: Any,
    method: str,
    safe_url: str,
    raw_url: str,
    source_ip: str,
    elapsed_ms: float,
    headers: Dict[str, Any],
    json_data: Optional[Any],
    data: Optional[Any],
) -> Any:
    """Logs a transient 5xx retry with brief backoff while strictly preserving source_ip."""
    ip_tag = f"with source_ip={source_ip}" if source_ip else "after 300ms backoff"
    logger.warning(f"⚠️  [PROXY {status_label}] {method} {safe_url} ({elapsed_ms:.1f}ms) — retrying {ip_tag}")
    await asyncio.sleep(0.3)
    return await _proxy_request_inner(method, raw_url, source_ip, headers, json_data, data, _is_retry=True)


async def _proxy_request_inner(
    method: str,
    url: str,
    source_ip: str,
    headers: Optional[Dict] = None,
    json_data: Optional[Any] = None,
    data: Optional[Any] = None,
    _is_retry: bool = False,
) -> Any:
    req_headers = dict(headers) if headers else {}

    # Strip X-Static-IP before forwarding if it exists (the proxy expects source_ip payload or handles it)
    req_headers.pop('X-Static-IP', None)

    payload: Dict[str, Any] = {
        "method": method,
        "url": url,
        "headers": req_headers
    }
    if source_ip:
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
    retry_tag = " [retry]" if _is_retry else ""
    req_body_str = json.dumps(json_data or data or {}, default=str) if (json_data is not None or data is not None) else "{}"
    sanitized_req = sanitize_log_message(req_body_str)
    logger.debug(f"📤 [PROXY REQ] {method} {safe_url} (source_ip={source_ip}){retry_tag}")
    await logger_api.info(f"📤 [PROXY REQ] {method} {safe_url} (source_ip={source_ip}){retry_tag} | Payload: {sanitized_req}")

    async with session.post(settings.DRMT_PROXY_URL, json=payload) as resp:
        already_logged = False
        try:
            resp_data = await resp.json()

            # HTTP 500 from proxy — retry once while preserving configured source_ip
            if resp.status == 500 and not _is_retry:
                elapsed_ms = (time.monotonic() - t0) * 1000
                return await _retry_proxy_request(
                    500, method, safe_url, url, source_ip, elapsed_ms, req_headers, json_data, data
                )

            resp.raise_for_status()
            elapsed_ms = (time.monotonic() - t0) * 1000

            # Check if this is wrapped by DRMT proxy
            if isinstance(resp_data, dict) and "status_code" in resp_data:
                target_sc = resp_data.get("status_code", 200)
                broker_body = resp_data.get("json") if resp_data.get("json") is not None else resp_data.get("text", "")

                # 500/502/503/504 wrapped response — retry once after backoff while preserving source_ip
                if target_sc in (500, 502, 503, 504) and not _is_retry:
                    return await _retry_proxy_request(
                        target_sc, method, safe_url, url, source_ip, elapsed_ms, req_headers, json_data, data
                    )

                # Parse JSON string if broker returned stringified JSON
                if isinstance(broker_body, str) and (broker_body.startswith("{") or broker_body.startswith("[")):
                    try:
                        broker_body = json.loads(broker_body)
                    except Exception:
                        pass

                resp_str = json.dumps(broker_body, default=str) if isinstance(broker_body, (dict, list)) else str(broker_body)
                if len(resp_str) > 50000:
                    resp_str = resp_str[:50000] + "... [truncated]"
                sanitized_resp = sanitize_log_message(resp_str)
                logger.debug(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_resp}")
                await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {target_sc} in {elapsed_ms:.1f}ms | Body: {sanitized_resp}")

                if target_sc in (401, 403):
                    already_logged = True
                    await logger_api.error(f"❌ [BROKER AUTH FAIL] {method} {safe_url} -> HTTP {target_sc} | Body: {sanitized_resp}")
                    raise PermissionError(f"Broker Auth Error (HTTP {target_sc}): {broker_body}")
                elif target_sc and target_sc >= 400:
                    already_logged = True
                    await logger_api.error(f"❌ [BROKER HTTP ERR] {method} {safe_url} -> HTTP {target_sc} | Body: {sanitized_resp}")
                    raise RuntimeError(f"Broker HTTP {target_sc} Error: {broker_body}")

                return broker_body
            else:
                resp_str = json.dumps(resp_data, default=str)
                if len(resp_str) > 5000:
                    resp_str = resp_str[:5000] + "... [truncated]"
                sanitized_resp = sanitize_log_message(resp_str)
                logger.debug(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_resp}")
                await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {resp.status} in {elapsed_ms:.1f}ms | Body: {sanitized_resp}")
                return resp_data
        except aiohttp.ContentTypeError:
            elapsed_ms = (time.monotonic() - t0) * 1000
            resp_text = await resp.text()

            # HTTP 500 text response — retry once while preserving configured source_ip
            if resp.status == 500 and not _is_retry:
                return await _retry_proxy_request(
                    500, method, safe_url, url, source_ip, elapsed_ms, req_headers, json_data, data
                )

            sanitized_text = sanitize_log_message(resp_text[:2000])
            logger.debug(f"📥 API Response ({elapsed_ms:.1f}ms): {sanitized_text}")
            await logger_api.info(f"📥 [PROXY RESP] {method} {safe_url} -> HTTP {resp.status} (text) in {elapsed_ms:.1f}ms | Body: {sanitized_text}")
            resp.raise_for_status()
            return resp_text
        except Exception as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            safe_err = sanitize_log_message(str(e))
            logger.debug(f"❌ API Error ({elapsed_ms:.1f}ms): {safe_err}")
            if not already_logged:
                await logger_api.error(f"❌ [PROXY ERR] {method} {safe_url} after {elapsed_ms:.1f}ms: {safe_err}")
            raise

