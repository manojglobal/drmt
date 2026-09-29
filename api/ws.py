import logging
import json
import time
from aiohttp import web
from api.auth import require_auth, get_client_ip
from core.engine import core_engine
from database.db import get_db
from models.schemas import Credential
from utils.user_mapping import get_configured_client_ids
from logger.logger import logger_api, logger_system

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
        valid_filter = "status = 'working' AND UPPER(broker) != 'FAKE' AND client_id IS NOT NULL AND TRIM(client_id) != ''"
        if user.username == 'admin':
            async with db.execute(f"SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE {valid_filter}") as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute(f"SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE {valid_filter} AND user_id = ?", (user.id,)) as cursor:
                rows = await cursor.fetchall()
            
    if not rows:
        # Don't close the connection, just let them sit connected with an empty list
        pass
        
    configured_cids = get_configured_client_ids()
    credentials = []
    for row in rows:
        broker_name = str(row[1] or "").strip().upper()
        cid = str(row[2] or "").strip()
        if not broker_name or broker_name == "FAKE" or not cid:
            continue
        if configured_cids and cid not in configured_cids:
            continue
        # Verify that credential has at least password, api_key, or token configured
        if not (row[3] or row[6] or row[10]):
            continue
        cred = Credential(
            id=row[0], broker=row[1], client_id=cid, api_key=row[3] or "", api_secret=row[4] or "",
            totp_secret=row[5] or "", password=row[6] or "", static_ip=row[7] or "",
            fingerprint=row[8], status=row[9], token=row[10] or "", token_updated_at=row[11]
        )
        credentials.append(cred)
    
    client_ip = get_client_ip(request)
    logger.info(f"🔌 [WS CONNECT] Client connected: user={user.username} (id={user.id}) from {client_ip}, active_creds={len(credentials)}")
    await logger_api.info(f"🔌 [WS CONNECT] user={user.username} (id={user.id}) ip={client_ip} creds={len(credentials)}")

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
        
    logger.info(f"🔌 [WS DISCONNECT] Client disconnected: user={user.username} (id={user.id}), closed={ws.closed}, close_code={ws.close_code}")
    await logger_api.info(f"🔌 [WS DISCONNECT] user={user.username} (id={user.id}) code={ws.close_code}")
        
    return ws
