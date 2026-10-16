import asyncio
import logging
import os
from aiohttp import web

from settings import settings
from core.engine import core_engine
from database.db import init_db, get_db
from api import auth_routes, api_routes, websocket_handler, rate_limit_middleware
from health.monitor import health_monitor
from logger.logger import logger_system, start_log_cleanup_loop, setup_logging
from credentials.crypto import hash_password
from utils.email import init_email_session, close_email_session
from utils.user_mapping import get_client_username, get_configured_client_ids

setup_logging()

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
    configured_cids = get_configured_client_ids()
    if not configured_cids:
        await logger_system.info("⚠️  No active broker credentials configured.")
        return

    placeholders = ",".join("?" for _ in configured_cids)
    async with get_db() as db:
        db.row_factory = None
        query = f"""
            SELECT broker, client_id, static_ip, status, api_key, token
            FROM credentials 
            WHERE status = 'working' 
              AND UPPER(broker) != 'FAKE' 
              AND client_id IN ({placeholders})
            ORDER BY broker, client_id
        """
        async with db.execute(query, list(configured_cids)) as cursor:
            rows = await cursor.fetchall()
            
    filtered_rows = [
        r for r in rows 
        if r[1] in configured_cids and (r[4] or r[5])
    ]
    if filtered_rows:
        await logger_system.info(f"─── [DRMT] Configured Broker Credentials ({len(filtered_rows)} Active) ───")
        for broker, client_id, static_ip, status, api_key, token in filtered_rows:
            name = get_client_username(client_id)
            ip_str = f" | IP: {static_ip}" if static_ip else ""
            name_str = f" | Name: {name}" if name and name != client_id else ""
            await logger_system.info(f"  ✔ [{broker}] Client: {client_id}{name_str}{ip_str}")
        await logger_system.info("───────────────────────────────────────────────────────")
    else:
        await logger_system.info("⚠️  No active broker credentials configured.")

async def authenticate_configured_credentials():
    """Authenticates configured broker accounts on startup."""
    from position.chronos import refresh_token_with_backoff
    from models.schemas import Credential
    from credentials.dynamic_loader import get_dynamic_credentials
    configured_cids = get_configured_client_ids()
    if not configured_cids:
        return [], [], []

    placeholders = ",".join("?" for _ in configured_cids)
    async with get_db() as db:
        db.row_factory = None
        query = f"""
            SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at, sid, base_url, sub_account_id, public_key, refresh_token, mobile_number, mpin
            FROM credentials 
            WHERE status = 'working' 
              AND UPPER(broker) != 'FAKE' 
              AND client_id IN ({placeholders})
            ORDER BY broker, client_id
        """
        async with db.execute(query, list(configured_cids)) as cursor:
            rows = await cursor.fetchall()
            
    filtered = [r for r in rows if r[2] in configured_cids]
    if not filtered:
        return [], [], []
        
    await logger_system.info(f"🔄 Authenticating {len(filtered)} configured account(s) on startup...")
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    auth_results: list[dict] = []
    from credentials.dynamic_loader import get_client_email
    from position.blocklist import get_blocked_user

    for r in filtered:
        broker_upper = str(r[1] or "").strip().upper()
        # Authenticate supported brokers
        if broker_upper in ("MSTOCK", "FLATTRADE", "ALICE BLUE", "ALICEBLUE", "FYERS", "KOTAK", "KOTAK NEO", "KOTAKNEO", "AXIS", "AXIS DIRECT", "AXISDIRECT"):
            sid_val = r[12] if len(r) > 12 else ""
            base_url_val = r[13] if len(r) > 13 else ""
            sub_acc_val = r[14] if len(r) > 14 else ""
            pub_key_val = r[15] if len(r) > 15 else ""
            ref_tok_val = r[16] if len(r) > 16 else ""
            dyn_data = get_dynamic_credentials(r[2]) or {}
            mob_val = r[17] if len(r) > 17 and r[17] else dyn_data.get("mobile_number") or ""
            mpin_val = r[18] if len(r) > 18 and r[18] else dyn_data.get("mpin") or ""
            cred = Credential(
                id=r[0], broker=r[1], client_id=r[2], api_key=r[3] or "", api_secret=r[4] or "",
                totp_secret=r[5] or "", password=r[6] or "", static_ip=r[7] or "",
                fingerprint=r[8], status=r[9], token=r[10] or "", token_updated_at=r[11],
                sid=sid_val or "", base_url=base_url_val or "",
                sub_account_id=sub_acc_val or "", public_key=pub_key_val or "", refresh_token=ref_tok_val or "",
                mobile_number=mob_val, mpin=mpin_val
            )
            acc_label = f"{cred.broker}:{cred.client_id}"
            user_email = get_client_email(cred.client_id)
            try:
                ok = await refresh_token_with_backoff(cred)
                if ok:
                    succeeded.append(acc_label)
                    auth_results.append({
                        "broker": cred.broker,
                        "client_id": cred.client_id,
                        "user_email": user_email,
                        "refreshed": True,
                        "reason": "",
                    })
                else:
                    blocked_info = get_blocked_user(cred.client_id)
                    reason_str = blocked_info.get("reason", "Failed") if blocked_info else "Failed"
                    failed.append((acc_label, reason_str))
                    auth_results.append({
                        "broker": cred.broker,
                        "client_id": cred.client_id,
                        "user_email": user_email,
                        "refreshed": False,
                        "reason": reason_str,
                    })
            except Exception as auth_err:
                reason_str = str(auth_err)
                failed.append((acc_label, reason_str))
                auth_results.append({
                    "broker": cred.broker,
                    "client_id": cred.client_id,
                    "user_email": user_email,
                    "refreshed": False,
                    "reason": reason_str,
                })
                logger.error(f"Startup auth error for {acc_label}: {auth_err}")
    return succeeded, failed, auth_results

async def on_startup(app: web.Application):
    """Application startup sequence."""
    await logger_system.info("Starting DRMT V2...")
    
    # 1. Boot CoreEngine
    await core_engine.start()
    
    # 2. Initialize DB Pool & Migrations
    await init_db()
    
    # 3. Dynamic Credentials Fetch & SQLite Sync from Remote URL
    from credentials.dynamic_loader import fetch_and_sync_credentials
    await fetch_and_sync_credentials()
    
    # 4. Initialize Email Session
    await init_email_session()
    
    # 5. Seed Admin User
    await seed_admin_user()
    
    # 6. Display Configured Credentials
    await display_configured_credentials()
    
    # 7. Initial One-Time Credential Verification & Authentication at Startup
    await logger_system.info("🔐 Performing initial one-time credential check and authentication...")
    succeeded, failed, auth_results = await authenticate_configured_credentials()
    await logger_system.info(f"🔐 Initial startup check complete: {len(succeeded)} ready, {len(failed)} failed/blocked.")
    
    # 8. Start Health Monitor
    await health_monitor.start()
    
    # 9. Start Daily Log Retention Cleanup (30 days)
    asyncio.create_task(start_log_cleanup_loop(retention_days=30), name="log_retention_cleanup")
    
    # 10. Start Daily Morning Credential Refresh Scheduler (09:10 AM IST)
    from credentials.scheduler import (
        start_daily_credential_scheduler,
        start_market_close_report_scheduler,
        start_axis_token_refresh_worker,
    )
    app["daily_scheduler_task"] = asyncio.create_task(start_daily_credential_scheduler(), name="daily_credential_scheduler")
    
    # 11. Start Market-Close Email Report Scheduler (03:30 PM IST)
    app["market_close_scheduler_task"] = asyncio.create_task(
        start_market_close_report_scheduler(),
        name="market_close_report_scheduler"
    )
    
    # 12. Start Blocked Credentials Recheck Worker (1-2 minutes during market hours)
    from credentials.watcher import start_blocked_credentials_watcher
    app["blocked_watcher_task"] = asyncio.create_task(
        start_blocked_credentials_watcher(interval_seconds=settings.BLOCKED_RECHECK_INTERVAL_SEC),
        name="blocked_credentials_watcher"
    )

    # 13. Start Axis Direct 13-Minute Silent Token Refresh Worker (Mon-Fri 08:30-16:00 IST)
    app["axis_refresh_task"] = asyncio.create_task(
        start_axis_token_refresh_worker(interval_seconds=780),
        name="axis_token_refresh_worker"
    )
    
    await logger_system.info("Application startup complete.")

async def on_cleanup(app: web.Application):
    """Application shutdown sequence."""
    await logger_system.info("Shutting down DRMT V2...")
    
    # 1. Stop Core Engine (cancels all per-user polling tasks, closes aiohttp & redis sessions)
    await core_engine.stop()
    
    # 2. Close Email Session
    await close_email_session()
    
    # 3. Stop Daily Scheduler Task
    if "daily_scheduler_task" in app and not app["daily_scheduler_task"].done():
        app["daily_scheduler_task"].cancel()

    # 4. Stop Market Close Scheduler Task
    if "market_close_scheduler_task" in app and not app["market_close_scheduler_task"].done():
        app["market_close_scheduler_task"].cancel()
        
    # 5. Stop Blocked Credentials Watcher Task
    if "blocked_watcher_task" in app and not app["blocked_watcher_task"].done():
        app["blocked_watcher_task"].cancel()

    # 6. Stop Axis Token Refresh Worker Task
    if "axis_refresh_task" in app and not app["axis_refresh_task"].done():
        app["axis_refresh_task"].cancel()
    
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
    
    # Register Embedded Frontend (In-Memory React SPA)
    from web.embedded import setup_embedded_frontend
    setup_embedded_frontend(app)
    
    return app

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    app = create_app()
    # Disable aiohttp access logs to prevent terminal spam from constant UI polling
    web.run_app(app, host="0.0.0.0", port=settings.PORT, access_log=None, print=logger.info)
