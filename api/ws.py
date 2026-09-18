import logging
import json
import time
from aiohttp import web
from api.auth import require_auth
from core.engine import core_engine
from database.db import get_db
from models.schemas import Credential

logger = logging.getLogger(__name__)

async def websocket_handler(request: web.Request):
    """
    Handles GET /ws/positions.
    Powers the React Green/Red indicator and prevents zombies.
    """
    try:
        user = await require_auth(request)
    except web.HTTPUnauthorized:
        return web.Response(status=401, text="Unauthorized")
        
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    # Send green connection status to frontend
    await ws.send_bytes(json.dumps({"type": "status", "connected": True}).encode("utf-8"))
    
    # We need to fetch the user's active credentials to pass to the polling loop.
    async with get_db() as db:
        db.row_factory = None
        if user.username == 'admin':
            async with db.execute("SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE status = 'working'") as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute("SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE status = 'working' AND user_id = ?", (user.id,)) as cursor:
                rows = await cursor.fetchall()
            
    if not rows:
        # Don't close the connection, just let them sit connected with an empty list
        pass
        
    credentials = []
    for row in rows:
        if "FAKE" in row[1].upper():
            continue
        cred = Credential(
            id=row[0], broker=row[1], client_id=row[2], api_key=row[3], api_secret=row[4],
            totp_secret=row[5], password=row[6], static_ip=row[7],
            fingerprint=row[8], status=row[9], token=row[10], token_updated_at=row[11]
        )
        credentials.append(cred)
    
    # Unique User Management: Core Engine handles cancelling old tasks
    await core_engine.spawn_user(str(user.id), ws, credentials)
    
    try:
        async for msg in ws:
            if msg.type in (web.WSMsgType.BINARY, web.WSMsgType.TEXT):
                try:
                    data_str = msg.data.decode("utf-8") if msg.type == web.WSMsgType.BINARY else msg.data
                    data = json.loads(data_str)
                    if data.get("type") == "ping":
                        core_engine.last_ping[str(user.id)] = time.monotonic()
                except Exception:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"WebSocket connection closed with exception {ws.exception()}")
    except Exception as e:
        logger.error(f"WebSocket Exception in async for loop: {e}")
        pass  # swallow disconnect errors - chronos loop handles cleanup
        
    logger.info(f"WebSocket loop ended for user {user.id}. closed={ws.closed}, close_code={ws.close_code}")
        
    return ws
