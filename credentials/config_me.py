import logging
from typing import List
from settings import settings
from core.engine import core_engine
from models.schemas import Credential
from credentials.dedup import save_working_credential

logger = logging.getLogger(__name__)

async def fetch_and_sync_config_me() -> int:
    """Fetches credentials from config.me and syncs them to SQLite."""
    session = core_engine.http_session
    if not session:
        raise RuntimeError("HTTP session not initialized")

    headers = {"Authorization": f"Bearer {settings.CONFIG_ME_API_KEY}"} if settings.CONFIG_ME_API_KEY else {}
    
    try:
        async with session.get(settings.CONFIG_ME_URL, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            
        credentials_data = data.get("credentials", []) if isinstance(data, dict) else data
        
        success_count = 0
        for item in credentials_data:
            cred = Credential(
                broker=item.get("broker", ""),
                client_id=item.get("client_id", ""),
                api_key=item.get("api_key", ""),
                api_secret=item.get("api_secret", ""),
                totp_secret=item.get("totp_secret", ""),
                password=item.get("password", ""),
                static_ip=item.get("static_ip", ""),
                fingerprint=item.get("fingerprint", f"{item.get('broker')}:{item.get('client_id')}"),
                status="working"
            )
            await save_working_credential(cred)
            success_count += 1
            
        logger.info(f"Successfully synced {success_count} credentials from config.me")
        return success_count
    except Exception as e:
        logger.error(f"Failed to sync from config.me: {e}")
        raise
