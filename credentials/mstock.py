import logging
from datetime import datetime
from models.schemas import Credential
from credentials.crypto import totp_now
from credentials.httpclient import proxy_request
from credentials.dedup import save_working_credential

logger = logging.getLogger("DRMT.SCM")

async def login_mstock(cred: Credential) -> str:
    """MStock 2-step login: password -> refresh_token -> totp -> jwt"""
    # Step 1: Login with password
    login_url = "https://api.mstock.trade/openapi/typeb/connect/login"
    login_headers = {
        "X-PrivateKey": cred.api_key,
        "X-Mirae-Version": "1",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    login_payload = {
        "clientcode": cred.client_id,
        "password": cred.password,
        "totp": "",
        "state": "",
    }
    
    resp1 = await proxy_request("POST", login_url, str(cred.static_ip), headers=login_headers, json_data=login_payload)
    data1 = resp1.get("data") if isinstance(resp1, dict) and isinstance(resp1.get("data"), dict) else resp1
    refresh_token = data1.get("refreshToken") if isinstance(data1, dict) else None
    if not refresh_token and isinstance(resp1, dict):
        refresh_token = resp1.get("refreshToken")
    if not refresh_token:
        raise ValueError(f"MStock login failed for {cred.client_id}: no refreshToken in response")
        
    # Step 2: Verify TOTP
    totp = totp_now(cred.totp_secret)
    totp_url = "https://api.mstock.trade/openapi/typeb/session/verifytotp"
    totp_headers = {
        "X-PrivateKey": cred.api_key,
        "X-Mirae-Version": "1",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    totp_payload = {"refreshToken": refresh_token, "totp": totp}
    
    resp2 = await proxy_request("POST", totp_url, str(cred.static_ip), headers=totp_headers, json_data=totp_payload)
    data2 = resp2.get("data") if isinstance(resp2, dict) and isinstance(resp2.get("data"), dict) else resp2
    jwt_token = data2.get("jwtToken") if isinstance(data2, dict) else None
    if not jwt_token and isinstance(resp2, dict):
        jwt_token = resp2.get("jwtToken")
    if not jwt_token:
        raise ValueError(f"MStock TOTP failed for {cred.client_id}: no jwtToken in response")
        
    cred.token = jwt_token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    
    masked = f"{jwt_token[:10]}...{jwt_token[-10:]}" if len(jwt_token) > 20 else jwt_token
    logger.info(f"🔑 Loaded dynamic auth token: {masked}")
    
    return jwt_token
