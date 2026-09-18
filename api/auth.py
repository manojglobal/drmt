from aiohttp import web
from datetime import datetime, timedelta
import json
from database.db import get_db
from credentials.crypto import verify_password, generate_session_token
from models.schemas import User
from logger.logger import logger_auth

routes = web.RouteTableDef()


@routes.post('/api/login')
async def login(request: web.Request):
    # Authenticates a user and issues a session cookie.
    try:
        data = await request.json()
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return web.json_response({"success": False, "message": "Missing credentials"}, status=400)

        async with get_db() as db:
            db.row_factory = None  # plain tuple
            async with db.execute(
                "SELECT id, username, password_hash, created_at FROM users WHERE username = ?",
                (username,)
            ) as cursor:
                row = await cursor.fetchone()

            if not row or not verify_password(password, row[2]):
                return web.json_response({"success": False, "message": "Invalid credentials"}, status=401)

            user_id = row[0]
            token = generate_session_token()
            expires = datetime.now() + timedelta(days=1)

            await db.execute(
                "INSERT INTO sessions (user_id, token, expires_at) VALUES (?, ?, ?)",
                (user_id, token, expires.isoformat()),
            )
            await db.commit()

        await logger_auth.info(f"[Auth] Admin login success for user '{username}'. Session created.")
        response = web.json_response({"success": True, "message": "ok"})
        response.set_cookie('drmt_session', token, httponly=True, samesite='Strict', path='/')
        return response

    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


@routes.post('/api/logout')
async def logout(request: web.Request):
    # Clears the user's session cookie and logs them out.
    token = request.cookies.get('drmt_session')
    if token:
        async with get_db() as db:
            await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            await db.commit()

    await logger_auth.info("[Auth] Logout: Session ended and cookies cleared.")
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
