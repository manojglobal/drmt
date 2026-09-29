import time
from aiohttp import web

from core.engine import core_engine
from database.db import get_db
from logger.logger import logger_api

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    # Try to identify user by session cookie, else IP
    # Intercepts HTTP requests to prevent spam or abuse.
    token = request.cookies.get('drmt_session')
    client_ip = request.headers.get("X-Forwarded-For", request.remote or "unknown_ip").split(",")[0].strip()
    user_identifier = client_ip
    
    if token:
        async with get_db() as db:
            async with db.execute("SELECT user_id FROM sessions WHERE token = ?", (token,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    user_identifier = f"user_{row[0]}"
                    
    await core_engine.acquire_rate_limit(user_identifier)

    # Operational lifecycle tracing for API routes
    t0 = time.monotonic()
    try:
        response = await handler(request)
        elapsed_ms = (time.monotonic() - t0) * 1000
        # Exclude static asset polls to keep log focused on operational APIs
        if request.path.startswith("/api"):
            await logger_api.info(
                f"[HTTP] {request.method} {request.path} -> {response.status} ({elapsed_ms:.1f}ms) [{user_identifier}]"
            )
        return response
    except Exception as e:
        elapsed_ms = (time.monotonic() - t0) * 1000
        if request.path.startswith("/api"):
            await logger_api.error(
                f"[HTTP] {request.method} {request.path} -> EXCEPTION {type(e).__name__}: {e} ({elapsed_ms:.1f}ms) [{user_identifier}]"
            )
        raise
