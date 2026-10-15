import json
import logging
from datetime import datetime
from typing import Dict, Any

from models.schemas import Credential
from credentials.httpclient import proxy_request
from core.engine import core_engine
from credentials.dedup import save_working_credential
from logger.logger import logger_auth, sanitize_log_message

logger = logging.getLogger("DRMT.SCM")

DEFAULT_KOTAK_BASE_URL = "https://napi.kotaksecurities.com"


def get_kotak_base_url(cred: Credential) -> str:
    """Returns the base URL configured for the Kotak Neo user or defaults to production NAPI."""
    base = getattr(cred, "base_url", "") or ""
    base = base.strip() if isinstance(base, str) else ""
    if not base:
        base = DEFAULT_KOTAK_BASE_URL
    return base.rstrip("/")


def get_kotak_headers(cred: Credential) -> Dict[str, str]:
    """
    Constructs the standard multi-header authentication parameters required by Kotak Neo Trade API:
    - Authorization: raw api_token (no Bearer prefix)
    - Sid: session user id / session token
    - Auth: active access token
    - neo-fin-key: standard constant 'neotradeapi'
    """
    api_token = (cred.api_key or "").strip()
    sid = (getattr(cred, "sid", "") or cred.password or "").strip()
    access_token = (cred.token or "").strip()

    return {
        "Authorization": api_token,
        "Sid": sid,
        "Auth": access_token,
        "neo-fin-key": "neotradeapi",
        "accept": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Kotak Neo Adapter)",
    }


async def login_kotakneo(cred: Credential) -> str:
    """
    Kotak Neo Session Validation & Verification.
    Validates active session credentials against Kotak Neo's GET /quick/user/positions.
    If valid, persists working status to SQLite and returns the access token.
    If expired/invalid, raises ValueError so user is safely quarantined to Failed Auth.
    """
    base_url = get_kotak_base_url(cred)
    headers = get_kotak_headers(cred)

    if not headers.get("Auth"):
        raise ValueError(f"Kotak Neo login failed for {cred.client_id}: access_token (Auth) is missing.")
    if not headers.get("Sid"):
        raise ValueError(f"Kotak Neo login failed for {cred.client_id}: Sid is missing.")

    positions_url = f"{base_url}/quick/user/positions"
    logger.info(f"Validating Kotak Neo session for {cred.client_id} via {positions_url}")

    try:
        if cred.static_ip:
            resp = await proxy_request("GET", positions_url, str(cred.static_ip), headers=headers)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.get(positions_url, headers=headers) as r:
                if r.status in (401, 403):
                    raise ValueError(f"Kotak Neo Auth Error: HTTP {r.status} Unauthorized")
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {"raw": resp}

        if isinstance(resp, dict):
            stat = str(resp.get("stat") or "").strip()
            st_code = resp.get("stCode")
            err_msg = str(resp.get("errMsg") or resp.get("message") or "")

            # Check known session invalidation codes
            if st_code in (100010, 401, 100008, 403, 1037) or stat.lower() == "not_ok":
                raise ValueError(
                    f"Kotak Neo session invalid for {cred.client_id} (stCode={st_code}, stat={stat}): {err_msg or 'Session expired'}"
                )

            # A valid response returns stat == "Ok" or a data list
            if stat == "Ok" or "data" in resp:
                cred.token_updated_at = datetime.now()
                await save_working_credential(cred)
                masked = f"{cred.token[:8]}...{cred.token[-8:]}" if len(cred.token) > 16 else cred.token
                logger.info(f"🔑 Validated Kotak Neo session token for {cred.client_id}: {masked}")
                return cred.token

        raise ValueError(f"Kotak Neo login failed for {cred.client_id}: unexpected response format {resp}")

    except Exception as e:
        if isinstance(e, ValueError):
            raise
        err_str = str(e)
        if any(code in err_str for code in ("100010", "401", "100008", "403", "1037", "Unauthorized", "Session expired")):
            raise ValueError(f"Kotak Neo Auth Error for {cred.client_id}: {err_str}")
        raise RuntimeError(f"Kotak Neo connection error for {cred.client_id}: {err_str}")
