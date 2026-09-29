from datetime import datetime, timedelta
import json
import logging

from aiohttp import web

from credentials.crypto import generate_session_token, verify_password
from database.db import get_db
from logger.logger import logger_auth, logger_audit
from models.schemas import User

logger = logging.getLogger("DRMT.Auth")
routes = web.RouteTableDef()


def get_client_ip(request: web.Request) -> str:
    """Safely extracts client IP address, checking proxy forwarding headers first."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


async def record_audit_log(
    user_id: int | None,
    username: str,
    action: str,
    status: str,
    ip_address: str,
    user_agent: str,
    details: str
) -> None:
    """Records authentication audit event in SQLite and rotating file data/logs/audit/."""
    try:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO auth_audit_logs 
                (user_id, username, action, status, ip_address, user_agent, details, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                """,
                (user_id, username, action, status, ip_address, user_agent[:255] if user_agent else "", details)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Failed to record auth audit log to SQLite: {e}")

    # Mirror to ServiceLogger("audit") -> data/logs/audit/app_YYYY-MM-DD.log
    await logger_audit.info(
        f"[AUDIT] user='{username}' (id={user_id or '-'}) | action={action} | status={status} | ip={ip_address} | {details}"
    )


@routes.post('/api/login')
async def login(request: web.Request):
    # Authenticates a user and issues a session cookie.
    ip = get_client_ip(request)
    ua = request.headers.get("User-Agent", "")
    try:
        data = await request.json()
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            await record_audit_log(None, username or "unknown", "LOGIN_FAILED", "FAILURE", ip, ua, "Missing credentials")
            return web.json_response({"success": False, "message": "Missing credentials"}, status=400)

        async with get_db() as db:
            db.row_factory = None  # plain tuple
            async with db.execute(
                "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
                (username,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                await record_audit_log(None, username, "LOGIN_FAILED", "FAILURE", ip, ua, "User does not exist")
                return web.json_response({"success": False, "message": "Invalid credentials"}, status=401)

            if not verify_password(password, row[2]):
                await record_audit_log(row[0], username, "LOGIN_FAILED", "FAILURE", ip, ua, "Invalid password")
                return web.json_response({"success": False, "message": "Invalid credentials"}, status=401)

            user_id = row[0]
            token = generate_session_token()
            expires = datetime.now() + timedelta(days=1)

            await db.execute(
                "INSERT INTO sessions (user_id, token, expires_at) VALUES (?, ?, ?)",
                (user_id, token, expires.isoformat()),
            )
            await db.commit()

        await record_audit_log(user_id, username, "LOGIN_SUCCESS", "SUCCESS", ip, ua, "Session token created")
        await logger_auth.info(f"[Auth] Login success for user '{username}'. Session created from {ip}.")
        response = web.json_response({"success": True, "message": "ok"})
        response.set_cookie('drmt_session', token, httponly=True, samesite='Strict', path='/')
        return response

    except Exception as e:
        await record_audit_log(None, "unknown", "LOGIN_ERROR", "FAILURE", ip, ua, f"Server error: {str(e)}")
        return web.json_response({"success": False, "message": str(e)}, status=500)


@routes.post('/api/logout')
async def logout(request: web.Request):
    # Clears the user's session cookie and logs them out.
    ip = get_client_ip(request)
    ua = request.headers.get("User-Agent", "")
    token = request.cookies.get('drmt_session')
    user_id = None
    username = "unknown"

    if token:
        async with get_db() as db:
            db.row_factory = None
            async with db.execute(
                "SELECT u.id, u.username FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?",
                (token,)
            ) as cursor:
                user_row = await cursor.fetchone()
                if user_row:
                    user_id, username = user_row[0], user_row[1]

            await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            await db.commit()

        await record_audit_log(user_id, username, "LOGOUT", "SUCCESS", ip, ua, "Session ended and deleted")
        await logger_auth.info(f"[Auth] Logout: user '{username}' session ended from {ip}.")

    response = web.json_response({"success": True})
    # MaxAge=-1 explicitly expires the cookie immediately
    response.set_cookie('drmt_session', '', max_age=-1, path='/')
    return response


@routes.get('/api/me')
async def me(request: web.Request):
    # Returns the currently authenticated user's details.
    try:
        user = await require_auth(request)
        return web.json_response({
            "success": True,
            "user": {"id": user.id, "username": user.username}
        })
    except web.HTTPUnauthorized as e:
        return web.Response(status=401, text=e.text, content_type='application/json')


async def require_auth(request: web.Request) -> User:
    """
    Validates the drmt_session cookie.
    Returns a fully populated User model on success.
    Raises web.HTTPUnauthorized on failure.
    """
    token = request.cookies.get('drmt_session')
    ip = get_client_ip(request)
    ua = request.headers.get("User-Agent", "")

    if not token:
        raise web.HTTPUnauthorized(
            text=json.dumps({"success": False, "message": "Unauthorized"}),
            content_type='application/json'
        )

    async with get_db() as db:
        # Join sessions + users to get the full user record in one query
        async with db.execute(
            """
            SELECT u.id, u.username, u.password_hash, u.created_at
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.token = ?
            """,
            (token,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await record_audit_log(None, "unauthorized", "SESSION_INVALID", "FAILURE", ip, ua, f"Invalid session token for {request.path}")
            raise web.HTTPUnauthorized(
                text=json.dumps({"success": False, "message": "Invalid session"}),
                content_type='application/json'
            )

        # Check expiry
        async with db.execute(
            "SELECT expires_at FROM sessions WHERE token = ?", (token,)
        ) as cursor:
            session_row = await cursor.fetchone()

        expires_str = session_row[0]
        expires = datetime.fromisoformat(expires_str) if isinstance(expires_str, str) else expires_str
        if datetime.now() > expires:
            await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            await db.commit()
            await record_audit_log(row[0], row[1], "SESSION_EXPIRED", "FAILURE", ip, ua, f"Session expired for {request.path}")
            raise web.HTTPUnauthorized(
                text=json.dumps({"success": False, "message": "Session expired"}),
                content_type='application/json'
            )

    return User(
        id=row[0],
        username=row[1],
        password_hash=row[2],
        created_at=datetime.fromisoformat(str(row[3])) if row[3] else datetime.now(),
    )
