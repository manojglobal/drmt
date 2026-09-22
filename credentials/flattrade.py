import logging
import uuid
from datetime import datetime
from models.schemas import Credential
from credentials.crypto import sha256_hex, totp_now
from credentials.httpclient import proxy_request
from credentials.dedup import save_working_credential
from core.engine import core_engine

logger = logging.getLogger("DRMT.SCM")

async def login_flattrade(cred: Credential) -> str:
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
    
    if cred.static_ip:
        resp = await proxy_request("POST", login_url, str(cred.static_ip), headers=headers, json_data=payload)
    else:
        session = core_engine.http_session
        if not session:
            raise RuntimeError("HTTP session not initialized")
        async with session.post(login_url, json=payload, headers=headers) as r:
            resp = await r.json(content_type=None)
            
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
        raise ValueError(f"Flattrade login failed for {cred.client_id}: {emsg}")
        
    token = quickauth.get("susertoken")
    if not token:
        raise ValueError(f"Flattrade login failed for {cred.client_id}: no susertoken in response")
        
    cred.token = token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    
    masked = f"{token[:10]}...{token[-10:]}" if len(token) > 20 else token
    logger.info(f"🔑 Loaded dynamic auth token: {masked}")
    
    return token
