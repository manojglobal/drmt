import logging
from datetime import datetime
from models.schemas import Credential
from credentials.crypto import sha256_hex
from credentials.httpclient import proxy_request
from credentials.dedup import save_working_credential

logger = logging.getLogger(__name__)

async def login_flattrade(cred: Credential) -> str:
    # Handles the full authentication flow for the Flattrade broker.
    logger.info(f"Logging in Flattrade {cred.client_id}")
    
    login_url = "https://emergeapi.flattrade.in/quickAuth"
    pwd_hash = sha256_hex(cred.password)
    
    payload = {
        "uid": cred.client_id,
        "pwd": pwd_hash,
        "api_key": cred.api_key
    }
    
    resp = await proxy_request("POST", login_url, str(cred.static_ip), json_data=payload)
    
    token = resp.get("quickauthresp", {}).get("susertoken")
    if not token:
        raise ValueError("Flattrade login failed: no susertoken")
        
    cred.token = token
    cred.token_updated_at = datetime.now()
    await save_working_credential(cred)
    
    return token
