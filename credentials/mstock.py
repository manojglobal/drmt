import logging
from datetime import datetime
from models.schemas import Credential
from credentials.crypto import totp_now
from credentials.httpclient import proxy_request
from credentials.dedup import save_working_credential

logger = logging.getLogger(__name__)

async def login_mstock(cred: Credential) -> str:
    """MStock 2-step login: password -> refresh_token -> totp -> jwt"""
    logger.info(f"Logging in MStock {cred.client_id}")
    
    # Step 1: Login with password
    login_url = "https://api.mstock.com/login"
    login_payload = {"user": cred.client_id, "pwd": cred.password}
    
    resp1 = await proxy_request("POST", login_url, str(cred.static_ip), json_data=login_payload)
    refresh_token = resp1.get("refreshToken")
    if not refresh_token:
        raise ValueError("MStock login failed: no refreshToken")
        
    # Step 2: Verify TOTP
    totp = totp_now(cred.totp_secret)
    totp_url = "https://api.mstock.com/verifytotp"
    totp_payload = {"user": cred.client_id, "totp": totp, "refreshToken": refresh_token}
    
    resp2 = await proxy_request("POST", totp_url, str(cred.static_ip), json_data=totp_payload)
    jwt_token = resp2.get("jwtToken")
    if not jwt_token:
        raise ValueError("MStock TOTP failed: no jwtToken")
        
    cred.token = jwt_token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    
    return jwt_token
