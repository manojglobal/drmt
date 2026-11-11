import json
import logging
import uuid
from datetime import datetime
from models.schemas import Credential
from credentials.crypto import sha256_hex, totp_now
from credentials.httpclient import proxy_request
from credentials.dedup import save_working_credential
from core.engine import core_engine
from settings import settings
from logger.logger import logger_auth, logger_api

logger = logging.getLogger("DRMT.SCM")

async def login_flattrade(cred: Credential) -> str:
    """Dispatches to Pi Connect SDK flow or Web quickAuth based on FLATTRADE_FLOW setting and credentials."""
    flow_mode = getattr(settings, "FLATTRADE_FLOW", "WEB").upper()
    is_sdk = (flow_mode == "SDK") and bool(cred.api_key and cred.api_secret)

    if is_sdk:
        return await login_flattrade_sdk(cred)
    return await login_flattrade_web(cred)


async def login_flattrade_sdk(cred: Credential) -> str:
    """Handles the 2-step headless OAuth authentication for Flattrade Pi Connect API."""
    logger.info(f"Logging in Flattrade (Pi SDK) {cred.client_id}")

    # Step 1: Headless user auth to obtain request_code
    totp = totp_now(cred.totp_secret) if cred.totp_secret else ""
    pwd_hash = sha256_hex(cred.password)

    ftauth_url = "https://authapi.flattrade.in/ftauth"
    payload1 = {
        "UserName": cred.client_id,
        "Password": pwd_hash,
        "Otp": totp
    }
    headers1 = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    }

    await logger_api.info(f"📤 [Flattrade SDK Auth Step 1 Request] ({cred.client_id}) -> POST {ftauth_url} | uid: {cred.client_id}, static_ip: {cred.static_ip}")
    if cred.static_ip:
        resp1 = await proxy_request("POST", ftauth_url, str(cred.static_ip), headers=headers1, json_data=payload1)
    else:
        session = core_engine.http_session
        if not session:
            raise RuntimeError("HTTP session not initialized")
        async with session.post(ftauth_url, json=payload1, headers=headers1) as r:
            resp1 = await r.json(content_type=None)

    if isinstance(resp1, str):
        try:
            resp1 = json.loads(resp1)
        except Exception:
            resp1 = {}

    resp1_str = json.dumps(resp1, default=str) if isinstance(resp1, (dict, list)) else str(resp1)
    await logger_api.info(f"📥 [Flattrade SDK Auth Step 1 Response] ({cred.client_id}): {resp1_str}")

    request_code = resp1.get("request_code") or resp1.get("code") if isinstance(resp1, dict) else None
    if not request_code:
        emsg = (resp1.get("emsg") or resp1.get("message") or resp1.get("errmsg")) if isinstance(resp1, dict) else "Missing request_code"
        await logger_api.error(f"❌ [Flattrade SDK Auth Step 1 Error] ({cred.client_id}): {emsg} | Full Response: {resp1_str}")
        raise ValueError(f"Flattrade SDK login Step 1 failed for {cred.client_id}: {emsg}")

    # Step 2: Compute signature SHA256(api_key + request_code + api_secret)
    sig = sha256_hex(f"{cred.api_key}{request_code}{cred.api_secret}")

    # Step 3: Token Exchange
    token_url = "https://authapi.flattrade.in/trade/apitoken"
    payload2 = {
        "api_key": cred.api_key,
        "request_code": request_code,
        "api_secret": sig
    }
    headers2 = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    }

    await logger_api.info(f"📤 [Flattrade SDK Auth Step 2 Request] ({cred.client_id}) -> POST {token_url} | uid: {cred.client_id}, static_ip: {cred.static_ip}")
    if cred.static_ip:
        resp2 = await proxy_request("POST", token_url, str(cred.static_ip), headers=headers2, json_data=payload2)
    else:
        async with core_engine.http_session.post(token_url, json=payload2, headers=headers2) as r:
            resp2 = await r.json(content_type=None)

    if isinstance(resp2, str):
        try:
            resp2 = json.loads(resp2)
        except Exception:
            resp2 = {}

    resp2_str = json.dumps(resp2, default=str) if isinstance(resp2, (dict, list)) else str(resp2)
    await logger_api.info(f"📥 [Flattrade SDK Auth Step 2 Response] ({cred.client_id}): {resp2_str}")

    stat = resp2.get("stat") if isinstance(resp2, dict) else None
    token = resp2.get("token") if isinstance(resp2, dict) else None
    if stat != "Ok" or not token:
        emsg = (resp2.get("emsg") or resp2.get("message") or resp2.get("errmsg")) if isinstance(resp2, dict) else "Token exchange failed"
        await logger_api.error(f"❌ [Flattrade SDK Auth Step 2 Error] ({cred.client_id}): {emsg} | Full Response: {resp2_str}")
        raise ValueError(f"Flattrade SDK login Step 2 failed for {cred.client_id}: {emsg}")

    cred.token = token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)

    masked = f"{token[:10]}...{token[-10:]}" if len(token) > 20 else token
    logger.info(f"🔑 Loaded dynamic auth token (Pi SDK): {masked}")
    await logger_api.info(f"🔑 [Flattrade SDK Auth Success] ({cred.client_id}): Token acquired ({masked})")

    return token


async def login_flattrade_web(cred: Credential) -> str:
    """Handles the full authentication flow for the Flattrade broker via quickAuth."""
    logger.info(f"Logging in Flattrade {cred.client_id}")
    
    login_url = "https://emergeapi.flattrade.in/quickAuth"
    pwd_hash = sha256_hex(cred.password)
    factor2 = totp_now(cred.totp_secret) if cred.totp_secret else ""
    
    payload = {
        "uid": cred.client_id,
        "pwd": pwd_hash,
        "factor2": factor2,
        "source": "WEB",
        "addldivinf": "Linux",
        "app_inst_id": str(uuid.uuid4()),
        "ipaddr": "127.0.0.1"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://emerge.flattrade.in",
        "Referer": "https://emerge.flattrade.in/",
        "device": "Chrome",
        "source": "WEB"
    }
    
    await logger_api.info(f"📤 [Flattrade Auth Request] ({cred.client_id}) -> POST {login_url} | uid: {cred.client_id}, source: WEB, static_ip: {cred.static_ip}")
    if cred.static_ip:
        resp = await proxy_request("POST", login_url, str(cred.static_ip), headers=headers, json_data=payload)
    else:
        session = core_engine.http_session
        if not session:
            raise RuntimeError("HTTP session not initialized")
        async with session.post(login_url, json=payload, headers=headers) as r:
            resp = await r.json(content_type=None)
            
    resp_str = json.dumps(resp, default=str) if isinstance(resp, (dict, list)) else str(resp)
    await logger_api.info(f"📥 [Flattrade QuickAuth Response] ({cred.client_id}): {resp_str}")
            
    data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
    quickauth = data.get("quickauthresp", {}) if isinstance(data, dict) else {}
    if not quickauth and isinstance(resp, dict):
        quickauth = resp.get("quickauthresp", {})
        
    stat = quickauth.get("stat") if isinstance(quickauth, dict) else None
    if stat != "Ok":
        emsg = (
            quickauth.get("emsg")
            if isinstance(quickauth, dict)
            else (data.get("errmsg") if isinstance(data, dict) else "Unknown login error")
        )
        await logger_api.error(f"❌ [Flattrade Auth Error] ({cred.client_id}): {emsg} | Full Response: {resp_str}")
        raise ValueError(f"Flattrade login failed for {cred.client_id}: {emsg}")
        
    token = quickauth.get("susertoken")
    if not token:
        await logger_api.error(f"❌ [Flattrade Auth Error] ({cred.client_id}): no susertoken in response | Full Response: {resp_str}")
        raise ValueError(f"Flattrade login failed for {cred.client_id}: no susertoken in response")
        
    cred.token = token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    
    masked = f"{token[:10]}...{token[-10:]}" if len(token) > 20 else token
    logger.info(f"🔑 Loaded dynamic auth token: {masked}")
    await logger_api.info(f"🔑 [Flattrade Auth Success] ({cred.client_id}): Token acquired ({masked})")
    
    return token
