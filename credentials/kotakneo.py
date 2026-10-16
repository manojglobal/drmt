import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from models.schemas import Credential
from credentials.httpclient import proxy_request
from core.engine import core_engine
from credentials.dedup import save_working_credential
from credentials.crypto import totp_now
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


async def generate_user_kotak_neo_access_token(
    client_id: str,
    mobile_number: str,
    mpin: str,
    api_token: str,
    totp_secret: str,
    static_ip: str,
) -> Dict[str, Any]:
    """
    Executes automated headless 2-step Kotak Neo login:
    Step 1: POST https://mis.kotaksecurities.com/login/1.0/tradeApiLogin
            (mobileNumber, ucc, live TOTP from totp_secret via pyotp) -> returns temp_token and sid
    Step 2: POST https://mis.kotaksecurities.com/login/1.0/tradeApiValidate
            (mpin, Auth: temp_token, sid) -> returns trade_token, trade_sid, baseUrl
    """
    try:
        client_id = str(client_id or "").strip()
        mobile_number = str(mobile_number or "").strip()
        mpin = str(mpin or "").strip()
        api_token = str(api_token or "").strip()
        totp_secret = str(totp_secret or "").strip().replace(" ", "")

        if not client_id or not api_token:
            return {"status": False, "msg": "Missing client_id or api_token for Kotak Neo login"}
        if not mobile_number or not mpin or not totp_secret:
            return {"status": False, "msg": f"Missing mobile_number, mpin, or totp_secret for {client_id}"}

        totp_code = totp_now(totp_secret)
        if not totp_code:
            return {"status": False, "msg": f"Failed to generate TOTP for {client_id}"}

        # Normalize mobile number: ensure +91 prefix
        digits_only = "".join(ch for ch in mobile_number if ch.isdigit())
        if digits_only.startswith("91") and len(digits_only) > 10:
            clean_mobile = f"+{digits_only}"
        else:
            clean_mobile = f"+91{digits_only}"

        login_url = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
        login_headers = {
            "Authorization": api_token,
            "neo-fin-key": "neotradeapi",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (DRMT Kotak Neo Adapter)",
        }
        login_payload = {
            "mobileNumber": clean_mobile,
            "ucc": client_id,
            "totp": totp_code,
        }

        logger.info(f"🔑 Initiating Kotak Neo Step 1 (tradeApiLogin) for {client_id} ({clean_mobile})")
        resp1 = await proxy_request("POST", login_url, static_ip, headers=login_headers, json_data=login_payload)
        if isinstance(resp1, str):
            try:
                resp1 = json.loads(resp1)
            except Exception:
                resp1 = {"raw": resp1}

        data1 = resp1.get("data") if isinstance(resp1, dict) and isinstance(resp1.get("data"), dict) else resp1
        temp_token = data1.get("token") if isinstance(data1, dict) else None
        sid = data1.get("sid") if isinstance(data1, dict) else None

        if not temp_token or not sid:
            err_detail = resp1.get("message") or resp1.get("errMsg") or str(resp1)
            return {"status": False, "msg": f"Kotak login Step 1 failed for {client_id}: {err_detail}"}

        logger.info(f"🔑 Kotak Neo Step 1 successful for {client_id}. Initiating Step 2 (tradeApiValidate)")
        validate_url = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
        validate_headers = {
            "Authorization": api_token,
            "neo-fin-key": "neotradeapi",
            "Content-Type": "application/json",
            "sid": sid,
            "Auth": temp_token,
            "User-Agent": "Mozilla/5.0 (DRMT Kotak Neo Adapter)",
        }
        validate_payload = {"mpin": mpin}

        try:
            resp2 = await proxy_request("POST", validate_url, static_ip, headers=validate_headers, json_data=validate_payload)
        except Exception:
            # Fallback: some Kotak gateway versions accept "Bearer " prefix
            validate_headers["Auth"] = f"Bearer {temp_token}"
            resp2 = await proxy_request("POST", validate_url, static_ip, headers=validate_headers, json_data=validate_payload)

        if isinstance(resp2, str):
            try:
                resp2 = json.loads(resp2)
            except Exception:
                resp2 = {"raw": resp2}

        data2 = resp2.get("data") if isinstance(resp2, dict) and isinstance(resp2.get("data"), dict) else resp2
        trade_token = data2.get("token") if isinstance(data2, dict) else None
        trade_sid = data2.get("sid") if isinstance(data2, dict) else None
        base_url = data2.get("baseUrl") if isinstance(data2, dict) else None

        if trade_token:
            return {
                "status": True,
                "token": trade_token,
                "sid": trade_sid or sid,
                "base_url": base_url or DEFAULT_KOTAK_BASE_URL,
            }
        else:
            err_detail = resp2.get("message") or resp2.get("errMsg") or str(resp2)
            return {"status": False, "msg": f"Kotak MPIN validate Step 2 failed for {client_id}: {err_detail}"}
    except Exception as e:
        return {"status": False, "msg": f"Kotak headless login exception for {client_id}: {e}"}


async def _execute_headless_kotak_login(cred: Credential) -> bool:
    """Helper to perform headless Kotak Neo login and update cred fields."""
    from credentials.dynamic_loader import get_dynamic_credentials, update_dynamic_credential_token
    dyn = get_dynamic_credentials(cred.client_id) or {}
    mob = getattr(cred, "mobile_number", "") or dyn.get("mobile_number") or ""
    mpin = getattr(cred, "mpin", "") or dyn.get("mpin") or ""
    totp_sec = cred.totp_secret or dyn.get("totp_secret") or ""
    api_tok = cred.api_key or dyn.get("api_key") or ""
    static_ip = str(cred.static_ip or dyn.get("static_ip") or "")

    res = await generate_user_kotak_neo_access_token(
        client_id=cred.client_id,
        mobile_number=mob,
        mpin=mpin,
        api_token=api_tok,
        totp_secret=totp_sec,
        static_ip=static_ip,
    )
    if res.get("status"):
        cred.token = res["token"]
        if res.get("sid"):
            cred.sid = res["sid"]
        if res.get("base_url"):
            cred.base_url = res["base_url"]
        cred.token_updated_at = datetime.now()
        await save_working_credential(cred)
        update_dynamic_credential_token(cred.client_id, cred.token)
        masked = f"{cred.token[:8]}...{cred.token[-8:]}" if len(cred.token) > 16 else cred.token
        logger.info(f"🔑 Successfully renewed Kotak Neo session token headlessly for {cred.client_id}: {masked}")
        return True
    else:
        logger.warning(f"❌ Headless Kotak Neo login failed for {cred.client_id}: {res.get('msg')}")
        return False


async def _probe_kotak_session(cred: Credential) -> Tuple[int, Any]:
    """Probes Kotak Neo positions endpoint to verify whether the session is currently alive."""
    base_url = get_kotak_base_url(cred)
    headers = get_kotak_headers(cred)
    positions_url = f"{base_url}/quick/user/positions"
    logger.info(f"Validating Kotak Neo session for {cred.client_id} via {positions_url}")

    try:
        if cred.static_ip:
            resp = await proxy_request("GET", positions_url, str(cred.static_ip), headers=headers)
            sc = 200
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("Core Engine HTTP session not initialized")
            async with session.get(positions_url, headers=headers) as r:
                sc = r.status
                try:
                    resp = await r.json(content_type=None)
                except Exception:
                    resp = await r.text()

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {"raw": resp}
        return sc, resp
    except PermissionError as pe:
        return 401, {"error": str(pe)}
    except Exception as e:
        err_s = str(e)
        if any(code in err_s for code in ("401", "403", "100010", "100022", "100008", "invalid session token", "Unauthorized")):
            return 401, {"error": err_s}
        return 500, {"error": err_s}


async def login_kotakneo(cred: Credential) -> str:
    """
    Kotak Neo Session Validation & Automated Recovery.
    1. If token or sid missing, immediately executes automated headless TOTP login.
    2. Probes GET /quick/user/positions to verify active session.
    3. If session expired or invalid (HTTP 401/403, stCode 100010/100022/100008), executes headless login.
    4. Re-validates session and saves working token to SQLite and cache.
    5. If still invalid, raises ValueError so user is safely quarantined.
    """
    sid_val = getattr(cred, "sid", "") or cred.password or ""
    if not cred.token or not sid_val:
        renewed = await _execute_headless_kotak_login(cred)
        if not renewed:
            raise ValueError(f"Kotak Neo login failed for {cred.client_id}: access_token or sid missing, and headless login failed.")

    sc, resp = await _probe_kotak_session(cred)
    stat = str(resp.get("stat") or "").strip() if isinstance(resp, dict) else ""
    st_code = resp.get("stCode") if isinstance(resp, dict) else None
    err_msg = str(resp.get("errMsg") or resp.get("message") or resp.get("error") or "") if isinstance(resp, dict) else ""
    is_no_data = "no data" in err_msg.lower() or "no data" in stat.lower()

    # Check for session invalidation (only if not a "No Data" 0-positions response)
    if not is_no_data and (sc in (401, 403) or st_code in (100010, 100022, 100008, 401, 403, 1037) or stat.lower() == "not_ok"):
        logger.info(f"🔑 Kotak Neo session expired for {cred.client_id} (sc={sc}, stCode={st_code}). Attempting automated headless renewal...")
        renewed = await _execute_headless_kotak_login(cred)
        if renewed:
            sc, resp = await _probe_kotak_session(cred)
            stat = str(resp.get("stat") or "").strip() if isinstance(resp, dict) else ""
            st_code = resp.get("stCode") if isinstance(resp, dict) else None
            err_msg = str(resp.get("errMsg") or resp.get("message") or resp.get("error") or "") if isinstance(resp, dict) else ""
            is_no_data = "no data" in err_msg.lower() or "no data" in stat.lower()

    if not is_no_data and (sc in (401, 403) or st_code in (100010, 100022, 100008, 401, 403, 1037) or stat.lower() == "not_ok"):
        msg = err_msg or "Session expired"
        raise ValueError(f"Kotak Neo Auth Error for {cred.client_id}: {msg}")

    # Success: persist working credential
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    masked = f"{cred.token[:8]}...{cred.token[-8:]}" if len(cred.token) > 16 else cred.token
    logger.info(f"🔑 Validated Kotak Neo session token for {cred.client_id}: {masked}")
    return cred.token
