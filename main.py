import asyncio
import logging
import os
from aiohttp import web

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)

from settings import settings
from core.engine import core_engine
from database.db import init_db, get_db
from api import auth_routes, api_routes, websocket_handler, rate_limit_middleware
from health.monitor import health_monitor
from logger.logger import logger_system, start_log_cleanup_loop
from credentials.crypto import hash_password
from utils.email import init_email_session, close_email_session

logger = logging.getLogger("DRMT.System")

async def seed_admin_user():
    """Seeds the default admin user if none exists."""
    async with get_db() as db:
        db.row_factory = None
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            row = await cursor.fetchone()
            if row and row[0] == 0:
                pwd_hash = hash_password("admin")
                await db.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, datetime('now', 'localtime'))",
                    ("admin", pwd_hash)
                )
                await db.commit()
                await logger_system.info("Seeded default admin user.")

async def display_configured_credentials():
    """Logs only configured and verified broker credentials to the terminal on startup."""
    from utils.user_mapping import get_client_username, get_configured_client_ids
    configured_cids = get_configured_client_ids()
    async with get_db() as db:
        db.row_factory = None
        query = """
            SELECT broker, client_id, static_ip, status
            FROM credentials 
            WHERE status = 'working' 
              AND UPPER(broker) != 'FAKE' 
              AND client_id IS NOT NULL 
              AND TRIM(client_id) != ''
            ORDER BY broker, client_id
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            
    filtered_rows = [r for r in rows if not configured_cids or r[1] in configured_cids]
    if filtered_rows:
        await logger_system.info(f"─── [DRMT] Configured Broker Credentials ({len(filtered_rows)} Active) ───")
        for broker, client_id, static_ip, status in filtered_rows:
            name = get_client_username(client_id)
            ip_str = f" | IP: {static_ip}" if static_ip else ""
            name_str = f" | Name: {name}" if name and name != client_id else ""
            await logger_system.info(f"  ✔ [{broker}] Client: {client_id}{name_str}{ip_str}")
        await logger_system.info("───────────────────────────────────────────────────────")
    else:
        await logger_system.info("⚠️  No active broker credentials configured.")

async def on_startup(app: web.Application):
    """Application startup sequence."""
    await logger_system.info("Starting DRMT V2...")
    
    # 1. Boot CoreEngine
    await core_engine.start()
    
    # 2. Initialize DB Pool & Migrations
    await init_db()
    
    # 3. Initialize Email Session
    await init_email_session()
    
    # 4. Seed Admin User
    await seed_admin_user()
    
    # 5. Display Configured Credentials
    await display_configured_credentials()
    
    # 6. Start Health Monitor
    await health_monitor.start()
    
    # 7. Start Daily Log Retention Cleanup (30 days)
    asyncio.create_task(start_log_cleanup_loop(retention_days=30), name="log_retention_cleanup")
    
    await logger_system.info("Application startup complete.")

async def on_cleanup(app: web.Application):
    """Application shutdown sequence."""
    await logger_system.info("Shutting down DRMT V2...")
    
    # 1. Stop Core Engine (cancels all per-user polling tasks, closes aiohttp & redis sessions)
    await core_engine.stop()
    
    # 2. Close Email Session
    await close_email_session()
    
    # DB Pool is managed by context managers, but we can assume safe closure as connections complete.
    await logger_system.info("Application shutdown complete.")

def create_app() -> web.Application:
    # Register Rate Limiting Middleware
    # Sets up the web application, routes, and middleware.
    app = web.Application(middlewares=[rate_limit_middleware])
    
    # Startup / Cleanup
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    
    # Register API Routes
    app.add_routes(auth_routes)
    app.add_routes(api_routes)
    
    # Register WebSocket Route
    app.router.add_get('/ws/positions', websocket_handler)
    
    # Health check route
    app.router.add_get('/api/health', lambda request: web.json_response(health_monitor.get_server_status()))
    
    # Register Static Files (React SPA)
    # web/ lives at project root: /opt/drmt/drmt/web
    project_root = os.path.dirname(os.path.abspath(__file__))
    web_dir = os.path.join(project_root, "web")
    os.makedirs(web_dir, exist_ok=True)
    index_path = os.path.join(web_dir, "index.html")
    if not os.path.exists(index_path):
        with open(index_path, "w") as f:
            f.write("<html><body><h1>DRMT V2 — Frontend Setup Required</h1></body></html>")

    app.router.add_static('/', web_dir, name='static', show_index=True)
    
    return app

if __name__ == "__main__":
    app = create_app()
    # Disable aiohttp access logs to prevent terminal spam from constant UI polling
    web.run_app(app, host="0.0.0.0", port=8000, access_log=None, print=logger.info)
