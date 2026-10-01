import json
import logging
from datetime import datetime

from core.engine import core_engine
from credentials.dedup import save_working_credential
from credentials.httpclient import proxy_request
from logger.logger import logger_auth, sanitize_log_message
from models.schemas import Credential

logger = logging.getLogger("DRMT.SCM")

FYERS_PROFILE_URL = "https://api-t1.fyers.in/api/v3/profile"


def get_fyers_auth_header(cred: Credential) -> str:
    """
    Formats the Fyers API v3 Authorization header.
    Fyers REST endpoints expect `{app_id}:{access_token}`.
    If cred.token already contains `app_id:`, returns cred.token.
    Otherwise prepends cred.api_key if present.
    """
    token = cred.token.strip() if cred.token else ""
    app_id = cred.api_key.strip() if cred.api_key else ""

    if not token:
        return ""
    if app_id and not token.startswith(f"{app_id}:"):
        return f"{app_id}:{token}"
    return token


async def login_fyers(cred: Credential) -> str:
    """
    Fyers OAuth2 Token Verification.
    Validates the active Fyers access_token against `GET /api/v3/profile`.
    If valid, updates credentials in SQLite and returns the token.
    If expired/invalid, raises ValueError so user is added to Blocklist safely.
    """
    auth_header = get_fyers_auth_header(cred)
    if not auth_header:
        raise ValueError(
            f"Fyers login failed for {cred.client_id}: access_token is missing. "
            "Please update credentials in CSV or database."
        )

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Fyers Adapter)",
    }

    try:
        if cred.static_ip:
            resp = await proxy_request("GET", FYERS_PROFILE_URL, str(cred.static_ip), headers=headers)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.get(FYERS_PROFILE_URL, headers=headers) as r:
                if r.status in (401, 403):
                    raise PermissionError(f"Fyers profile request rejected (HTTP {r.status})")
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}

        if not isinstance(resp, dict):
            raise ValueError(f"Fyers login failed for {cred.client_id}: unexpected response {resp}")

        # Check response status
        s_val = str(resp.get("s", "")).lower()
        code_val = resp.get("code")
        msg = resp.get("message") or resp.get("errMsg") or ""

        if s_val == "ok" or code_val == 200:
            cred.token_updated_at = datetime.now()
            await save_working_credential(cred)

            masked = (
                f"{cred.token[:10]}...{cred.token[-10:]}"
                if len(cred.token) > 20
                else sanitize_log_message(cred.token)
            )
            logger.info(f"🔑 Loaded dynamic auth token for FYERS:{cred.client_id}: {masked}")
            await logger_auth.info(f"✅ Fyers profile verified for {cred.client_id} [token: {masked}]")
            return cred.token
        else:
            raise ValueError(
                f"Fyers authentication failed for {cred.client_id} (code={code_val}, message='{msg}'). "
                "Token expired or invalid — please refresh access_token in CSV."
            )

    except PermissionError as pe:
        raise ValueError(
            f"Fyers token expired or unauthorized for {cred.client_id}: {pe}. "
            "Please update access_token in credentials CSV."
        )
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Fyers login check error for {cred.client_id}: {e}")
