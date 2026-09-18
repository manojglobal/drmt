import aiohttp
from typing import Dict, Any, Optional
from settings import settings
from core.engine import core_engine

async def proxy_request(method: str, url: str, source_ip: str, headers: Optional[Dict] = None, json_data: Optional[Dict] = None) -> Any:
    """Wraps aiohttp to send requests via the DRMT proxy."""
    if not headers:
        headers = {}
        
    # Strip X-Static-IP before forwarding if it exists (the proxy expects source_ip payload or handles it)
    headers.pop('X-Static-IP', None)
    
    payload = {
        "method": method,
        "url": url,
        "source_ip": source_ip,
        "headers": headers
    }
    if json_data:
        payload["json"] = json_data
        
    session = core_engine.http_session
    if not session:
        raise RuntimeError("Core Engine HTTP session not initialized")
        
    async with session.post(settings.DRMT_PROXY_URL, json=payload) as resp:
        resp.raise_for_status()
        return await resp.json()
