"""
credentials/axisdirect.py — Axis Direct Authentication & Cryptographic Handshake Module for DRMT V2.
Handles:
1. RSA Public Key Handshake via https://welcome-api.axisdirect.in/handshake
2. Standard multi-header parameter generation (authorization_key, api_client_id, sub_account_id, access_token, public_key)
3. Session validation via order-book / position probe
4. In-flight token renewal and session quarantine
"""

import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from models.schemas import Credential
from credentials.httpclient import proxy_request
from core.engine import core_engine
from credentials.dedup import save_working_credential
from logger.logger import logger_auth, sanitize_log_message

logger = logging.getLogger("DRMT.SCM")

AXIS_WELCOME_BASE_URL = "https://welcome-api.axisdirect.in"
AXIS_INVEST_BASE_URL = "https://invest-api.axisdirect.in"

# In-memory public key cache: api_client_id -> public_key
_axis_public_keys: Dict[str, str] = {}


async def get_axis_public_key(api_client_id: str, authorization_key: str, static_ip: str) -> str:
    """
    Fetches the RSA Public Key from Axis Direct via POST /handshake.
    Caches the public key in memory to eliminate repeated network handshakes.
    """
    cid = str(api_client_id or "").strip()
    if cid in _axis_public_keys and _axis_public_keys[cid]:
        return _axis_public_keys[cid]

    handshake_url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/handshake"
    headers = {
        "api_client_id": cid,
        "authorization_key": str(authorization_key or "").strip(),
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Axis Direct Handshake)",
    }

    try:
        if static_ip:
            resp = await proxy_request("POST", handshake_url, str(static_ip), headers=headers, json_data={})
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.post(handshake_url, headers=headers, json={}) as r:
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}

        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            pub_key = (
                data.get("publicKey")
                or data.get("public_key")
                or data.get("key")
                or resp.get("publicKey")
                or resp.get("public_key")
                or ""
            )
            if pub_key:
                pub_key = str(pub_key).strip()
                _axis_public_keys[cid] = pub_key
                logger.info(f"🔑 Successfully acquired Axis Direct public key for {cid}")
                return pub_key

        logger.warning(f"Axis Direct handshake returned unexpected format: {resp}")
        return ""
    except Exception as e:
        logger.error(f"❌ Failed to fetch Axis Direct public key for {cid}: {e}")
        return ""


def get_axis_headers(
    api_client_id: str,
    authorization_key: str,
    sub_account_id: str,
    access_token: str,
    public_key: str,
) -> Dict[str, str]:
    """
    Constructs the standard authentication headers required by Axis Direct RAPID API:
    - authorization_key
    - api_client_id
    - sub_account_id
    - access_token
    - public_key
    """
    return {
        "authorization_key": str(authorization_key or "").strip(),
        "api_client_id": str(api_client_id or "").strip(),
        "sub_account_id": str(sub_account_id or "").strip(),
        "access_token": str(access_token or "").strip(),
        "public_key": str(public_key or "").strip(),
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Axis Direct Adapter)",
    }


async def send_axis_api_request(
    method: str,
    url: str,
    static_ip: str,
    authorization_key: str,
    api_client_id: str,
    sub_account_id: str,
    access_token: str,
    public_key: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """
    Dispatches an HTTP request to the Axis Direct gateway via the DRMT proxy or direct HTTP session.
    Returns (status_code, response_data).
    """
    headers = get_axis_headers(api_client_id, authorization_key, sub_account_id, access_token, public_key)
    try:
        if static_ip:
            resp = await proxy_request(method, url, str(static_ip), headers=headers, json_data=body or {})
            if isinstance(resp, str):
                try:
                    resp = json.loads(resp)
                except Exception:
                    pass
            # If proxy returned a normal response without throwing PermissionError
            return 200, resp
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.request(method, url, headers=headers, json=body or {}) as r:
                sc = r.status
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = await r.text()
                return sc, data
    except PermissionError as pe:
        # Proxy identified 401 or 403
        return 401, {"error": {"code": "401", "message": str(pe)}}
    except Exception as e:
        err_s = str(e)
        if any(code in err_s for code in ("401", "403", "1081", "1082", "1200", "1002", "1003")):
            return 401, {"error": {"code": "401", "message": err_s}}
        return 500, {"error": {"message": err_s}}


async def refresh_user_axis_direct_access_token(cred: Credential) -> bool:
    """
    Attempts to renew an expired access_token using the stored refresh_token.
    Endpoint: POST {AXIS_WELCOME_BASE_URL}/auth/token/refresh
    """
    refresh_token = getattr(cred, "refresh_token", "") or ""
    if not refresh_token:
        logger.debug(f"Axis Direct refresh token missing for {cred.client_id}; cannot execute in-flight renewal.")
        return False

    url = f"{AXIS_WELCOME_BASE_URL.rstrip('/')}/auth/token/refresh"
    headers = {
        "api_client_id": cred.api_key,
        "authorization_key": cred.api_secret,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Axis Direct Token Refresh)",
    }
    payload = {"refreshToken": refresh_token}

    try:
        if cred.static_ip:
            resp = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.post(url, headers=headers, json=payload) as r:
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}

        data = resp.get("data", {}) if isinstance(resp.get("data"), dict) else resp
        new_token = data.get("access_token") or data.get("accessToken") or data.get("token")
        new_refresh = data.get("refresh_token") or data.get("refreshToken")

        if new_token:
            cred.token = str(new_token).strip()
            if new_refresh:
                cred.refresh_token = str(new_refresh).strip()
            cred.token_updated_at = datetime.now()
            await save_working_credential(cred)
            logger.info(f"🔑 Successfully renewed Axis Direct access token for {cred.client_id}")
            return True

        logger.warning(f"Axis Direct token renewal failed for {cred.client_id}: {resp}")
        return False
    except Exception as e:
        logger.error(f"❌ Axis Direct token renewal exception for {cred.client_id}: {e}")
        return False


async def login_axisdirect(cred: Credential) -> str:
    """
    Axis Direct Session Validation & Verification.
    Validates credentials against Axis Direct invest gateway.
    1. Ensures Public Key is present (fetches via handshake if missing).
    2. Probes GET /trading/orders/get-order-book or /positions.
    3. If session expired, attempts automatic in-flight token renewal.
    4. If still invalid, raises ValueError so account is safely placed in Failed Auth.
    """
    api_client_id = cred.api_key or ""
    authorization_key = cred.api_secret or ""
    sub_account_id = getattr(cred, "sub_account_id", "") or cred.client_id or ""
    access_token = cred.token or ""
    public_key = getattr(cred, "public_key", "") or ""

    if not api_client_id or not authorization_key:
        raise ValueError(f"Axis Direct credentials missing for {cred.client_id}: api_client_id or authorization_key not configured.")

    if not public_key:
        public_key = await get_axis_public_key(api_client_id, authorization_key, str(cred.static_ip or ""))
        if public_key:
            cred.public_key = public_key

    if not access_token or not public_key:
        # Try renewal via refresh token
        renewed = await refresh_user_axis_direct_access_token(cred)
        if renewed:
            access_token = cred.token
            if not public_key:
                public_key = await get_axis_public_key(api_client_id, authorization_key, str(cred.static_ip or ""))
                cred.public_key = public_key

    if not access_token:
        raise ValueError(f"Axis Direct login failed for {cred.client_id}: access_token is missing.")
    if not public_key:
        raise ValueError(f"Axis Direct login failed for {cred.client_id}: public_key could not be acquired.")

    # Probe orderbook to verify active session
    probe_url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/trading/orders/get-order-book"
    status_code, resp = await send_axis_api_request(
        method="POST",
        url=probe_url,
        static_ip=str(cred.static_ip or ""),
        authorization_key=authorization_key,
        api_client_id=api_client_id,
        sub_account_id=sub_account_id,
        access_token=access_token,
        public_key=public_key,
        body={"segment": "FO"},
    )

    err_dict = resp.get("error", {}) if isinstance(resp, dict) else {}
    err_code = str(err_dict.get("code") or "")

    # Session invalidation error codes: 401, 403, 1081, 1082, 1200, 1002, 1003
    if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
        # Attempt renewal once
        renewed = await refresh_user_axis_direct_access_token(cred)
        if renewed:
            status_code, resp = await send_axis_api_request(
                method="POST",
                url=probe_url,
                static_ip=str(cred.static_ip or ""),
                authorization_key=authorization_key,
                api_client_id=api_client_id,
                sub_account_id=sub_account_id,
                access_token=cred.token,
                public_key=cred.public_key,
                body={"segment": "FO"},
            )
            err_dict = resp.get("error", {}) if isinstance(resp, dict) else {}
            err_code = str(err_dict.get("code") or "")

    if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
        msg = err_dict.get("message") or f"Session error code {err_code}"
        raise ValueError(f"Axis Direct Auth Error for {cred.client_id}: {msg}")

    # Success: persist working credential
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    masked = f"{cred.token[:8]}...{cred.token[-8:]}" if len(cred.token) > 16 else cred.token
    logger.info(f"🔑 Validated Axis Direct session token for {cred.client_id}: {masked}")
    return cred.token
