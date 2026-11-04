import json
import logging
from datetime import datetime
from models.schemas import Credential
from credentials.crypto import totp_now
from credentials.httpclient import proxy_request
from credentials.dedup import save_working_credential
from logger.logger import logger_auth, logger_api

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
    
    await logger_api.info(f"📤 [mStock Login Step 1 Request] ({cred.client_id}) -> POST {login_url} | clientcode: {cred.client_id}, static_ip: {cred.static_ip}")
    resp1 = await proxy_request("POST", login_url, str(cred.static_ip), headers=login_headers, json_data=login_payload)
    resp1_str = json.dumps(resp1, default=str) if isinstance(resp1, (dict, list)) else str(resp1)
    await logger_api.info(f"📥 [mStock Login Step 1 Response] ({cred.client_id}): {resp1_str}")
    data1 = resp1.get("data") if isinstance(resp1, dict) and isinstance(resp1.get("data"), dict) else resp1
    refresh_token = data1.get("refreshToken") if isinstance(data1, dict) else None
    if not refresh_token and isinstance(resp1, dict):
        refresh_token = resp1.get("refreshToken")
    if not refresh_token:
        err_msg = resp1.get("message") or resp1.get("errMsg") or "no refreshToken in response" if isinstance(resp1, dict) else str(resp1)
        await logger_api.error(f"❌ [mStock Login Step 1 Error] ({cred.client_id}): {err_msg} | Full Response: {resp1_str}")
        raise ValueError(f"MStock login failed for {cred.client_id}: {err_msg}")
        
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
    
    await logger_api.info(f"📤 [mStock VerifyTOTP Step 2 Request] ({cred.client_id}) -> POST {totp_url}")
    resp2 = await proxy_request("POST", totp_url, str(cred.static_ip), headers=totp_headers, json_data=totp_payload)
    resp2_str = json.dumps(resp2, default=str) if isinstance(resp2, (dict, list)) else str(resp2)
    await logger_api.info(f"📥 [mStock VerifyTOTP Step 2 Response] ({cred.client_id}): {resp2_str}")
    data2 = resp2.get("data") if isinstance(resp2, dict) and isinstance(resp2.get("data"), dict) else resp2
    jwt_token = data2.get("jwtToken") if isinstance(data2, dict) else None
    if not jwt_token and isinstance(resp2, dict):
        jwt_token = resp2.get("jwtToken")
    if not jwt_token:
        err_msg = resp2.get("message") or resp2.get("errMsg") or "no jwtToken in response" if isinstance(resp2, dict) else str(resp2)
        await logger_api.error(f"❌ [mStock VerifyTOTP Step 2 Error] ({cred.client_id}): {err_msg} | Full Response: {resp2_str}")
        raise ValueError(f"MStock TOTP failed for {cred.client_id}: {err_msg}")
        
    cred.token = jwt_token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    
    masked = f"{jwt_token[:10]}...{jwt_token[-10:]}" if len(jwt_token) > 20 else jwt_token
    logger.info(f"🔑 Loaded dynamic auth token: {masked}")
    await logger_api.info(f"🔑 [mStock Auth Success] ({cred.client_id}): Token acquired ({masked})")
    
    return jwt_token
