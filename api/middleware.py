import json
from aiohttp import web
from core.engine import core_engine
from database.db import get_db
from datetime import datetime

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    # Try to identify user by session cookie, else IP
    # Intercepts HTTP requests to prevent spam or abuse.
    token = request.cookies.get('drmt_session')
    user_identifier = request.remote or "unknown_ip"
    
    if token:
        async with get_db() as db:
            async with db.execute("SELECT user_id FROM sessions WHERE token = ?", (token,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    user_identifier = f"user_{row[0]}"
                    
    await core_engine.acquire_rate_limit(user_identifier)
    return await handler(request)
