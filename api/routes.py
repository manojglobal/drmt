import csv
import json
import logging
import os
from dataclasses import asdict
from aiohttp import web
from api.auth import require_auth
from database.db import get_db
from models.schemas import Credential, Position
from position.chronos import refresh_token_with_backoff
from execution.engine import mass_exit
from utils.user_mapping import get_client_username, get_client_ids_for_user, get_configured_client_ids
from position.blocklist import get_blocked_users, get_blocked_user
from logger.logger import (
    logger_auth, logger_exit, logger_system, logger_monitor,
    logger_health, logger_audit, logger_api
)

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

@routes.get('/api/credentials/source')
async def get_credentials_source(request: web.Request):
    """Returns the current credentials source (Dropbox vs. Local CSV)."""
    await require_auth(request)
    from credentials.dynamic_loader import get_credential_source_info
    return web.json_response({"success": True, **get_credential_source_info()})


@routes.post('/api/credentials/source')
async def update_credentials_source(request: web.Request):
    """
    Switches credential source between Dropbox and Local CSV dynamically at runtime.
    Body: {"use_dropbox": true/false, "dropbox_url": "https://..."}
    """
    await require_auth(request)
    try:
        data = await request.json()
    except Exception:
        data = {}

    use_dropbox = data.get("use_dropbox")
    dropbox_url = data.get("dropbox_url")

    if use_dropbox is not None:
        settings.USE_DROPBOX_CREDENTIALS = bool(use_dropbox)
    if dropbox_url is not None:
        settings.DROPBOX_CREDENTIALS_URL = str(dropbox_url).strip()

    from credentials.dynamic_loader import fetch_and_sync_credentials, get_credential_source_info
    synced_count = await fetch_and_sync_credentials()
    source_info = get_credential_source_info()

    return web.json_response({
        "success": True,
        "message": f"Credential source updated to {source_info['active_source']}. Synced {synced_count} accounts.",
        **source_info
    })


@routes.post('/api/credentials/refresh')
async def refresh_credentials(request: web.Request):
    """
    Credential Refresh Button: Syncs credentials from active source (Dropbox / CSV)
    and forces token refresh without server restart.
    """
    user = await require_auth(request)

    # 1. Re-sync fresh credentials from the active source (Dropbox or CSV)
    try:
        from credentials.dynamic_loader import fetch_and_sync_credentials
        await fetch_and_sync_credentials()
    except Exception as e:
        logger.error(f"Error syncing credentials during refresh: {e}")
    
    # 2. Authenticate configured broker credentials
    from main import authenticate_configured_credentials
    succeeded, failed, auth_results = await authenticate_configured_credentials()

    # 3. Optional tabular email dispatch
    if auth_results:
        from credentials.scheduler import build_refresh_tables_html
        from utils.email import send_email_notification, send_error_email
        plain_table, html_table = build_refresh_tables_html(auth_results)
        if failed:
            asyncio.create_task(
                send_error_email(f"Manual Credential Refresh Alert: {len(failed)} Failed", plain_table, html_body=html_table),
                name="manual_refresh_alert"
            )
        elif succeeded:
            asyncio.create_task(
                send_email_notification(f"Manual Credential Refresh Successful: {len(succeeded)} Ready", plain_table, html_body=html_table),
                name="manual_refresh_notice"
            )

    return web.json_response({
        "success": True,
        "message": f"Credential refresh complete: {len(succeeded)} succeeded, {len(failed)} failed.",
        "refreshed": len(succeeded),
        "failed": len(failed),
        "total": len(auth_results),
        "details": auth_results
    })

@routes.post('/api/exit')
async def exit_positions(request: web.Request):
    """
    Handles mass exit of positions.
    Includes fallbacks for exit_price and trans_type.
    """
    user = await require_auth(request)
    data = await request.json()
    raw_positions = data.get("positions", [])
    
    if not raw_positions:
        return web.json_response({"success": False, "message": "No positions provided"})
        
    positions = []
    for rp in raw_positions:
        # Fallbacks before Pydantic validation
        if not rp.get("transaction_type"):
            qty = rp.get("quantity", 0)
            rp["transaction_type"] = "BUY" if qty > 0 else "SELL"
            
        pos = Position(**rp)
        if pos.ltp == 0:
            logger.info(f"Fallback exit_price to avg_price ({pos.avg_price}) for {pos.symbol}")
        positions.append(pos)
        
    # Fetch active credentials for these positions belonging to the user
    async with get_db() as db:
        db.row_factory = None
        valid_filter = "status = 'working' AND UPPER(broker) != 'FAKE' AND client_id IS NOT NULL AND TRIM(client_id) != ''"
        async with db.execute(f"SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE {valid_filter}") as cursor:
            rows = await cursor.fetchall()
            
    configured_cids = get_configured_client_ids()
    credentials = [Credential(
        id=r[0], broker=r[1], client_id=r[2], api_key=r[3], api_secret=r[4],
        totp_secret=r[5], password=r[6], static_ip=r[7],
        fingerprint=r[8], status=r[9], token=r[10], token_updated_at=r[11]
    ) for r in rows if not configured_cids or r[2] in configured_cids]
    
    symbol_list = [f"{p.broker}:{p.client_id}:{p.symbol}" for p in positions]
    await logger_exit.info(
        f"🚨 [EXIT TRIGGERED] user='{user.username}' (id={user.id}) | Total: {len(positions)} position(s) | Targets: {symbol_list}"
    )

    success_count, results = await mass_exit(positions, credentials)
    
    # Write only successfully exited positions to SQLite
    async with get_db() as db:
        for r in results:
            if r.get("success"):
                pos = r["position"]
                exit_price = pos.ltp if pos.ltp != 0 else pos.avg_price
                await db.execute("""
                    INSERT INTO exited_positions 
                    (position_id, broker, client_id, symbol, token, exchange, product_type, order_type, transaction_type, quantity, avg_price, exit_price, pnl, exited_at) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                """, (pos.id, pos.broker, pos.client_id, pos.symbol, pos.token, pos.exchange, pos.product_type, pos.order_type, pos.transaction_type, pos.quantity, pos.avg_price, exit_price, pos.pnl))
        await db.commit()
        
    client_results = [
        {
            "id": r["position"].id if r.get("position") else "",
            "broker": r["broker"],
            "client_id": r["position"].client_id if r.get("position") else "",
            "symbol": r["symbol"],
            "success": r["success"],
            "message": r["message"]
        }
        for r in results
    ]

    await logger_exit.info(
        f"🏁 [EXIT SUMMARY] user='{user.username}' (id={user.id}) | Exited: {success_count}/{len(positions)} | Detailed Results: {json.dumps(client_results, default=str)}"
    )

    return web.json_response({
        "success": success_count > 0 or len(positions) == 0,
        "message": f"Exited {success_count}/{len(positions)} positions",
        "results": client_results
    })

@routes.get('/api/exited/dates')
async def get_exited_dates(request: web.Request):
    """Returns distinct dates from exited_positions for frontend date picker."""
    await require_auth(request)
    async with get_db() as db:
        async with db.execute("SELECT DISTINCT date(exited_at) as dt FROM exited_positions WHERE exited_at IS NOT NULL ORDER BY dt DESC") as cursor:
            rows = await cursor.fetchall()
            dates = [r[0] for r in rows if r[0]]
    return web.json_response(dates)


@routes.get('/api/exited')
async def get_exited_positions(request: web.Request):
    """
    Exited filter rule: If broker or client is "ALL", skip SQL filter.
    """
    await require_auth(request)
    
    broker = request.query.get("broker", "ALL")
    client_id = request.query.get("client_id", "ALL")
    from_date = request.query.get("from_date")
    to_date = request.query.get("to_date")
    
    query = "SELECT * FROM exited_positions WHERE 1=1"
    params = []
    
    if broker != "ALL":
        query += " AND broker = ?"
        params.append(broker)
        
    if client_id != "ALL":
        cids = get_client_ids_for_user(client_id)
        if len(cids) == 1:
            query += " AND client_id = ?"
            params.append(cids[0])
        else:
            placeholders = ",".join("?" for _ in cids)
            query += f" AND client_id IN ({placeholders})"
            params.extend(cids)
            
    if from_date:
        query += " AND date(exited_at) >= date(?)"
        params.append(from_date)
        
    if to_date:
        query += " AND date(exited_at) <= date(?)"
        params.append(to_date)
        
    query += " ORDER BY exited_at DESC"
    
    async with get_db() as db:
        db.row_factory = None # For simplicity, returning raw dicts in production we'd use aiosqlite.Row
        async with db.execute(query, params) as cursor:
            columns = [desc[0] for desc in cursor.description]
            rows = await cursor.fetchall()
            result = [dict(zip(columns, row)) for row in rows]
            for r in result:
                r["username"] = get_client_username(r.get("client_id", ""))
                pt = str(r.get("product_type", "")).upper()
                if pt in ("I", "MIS"):
                    r["product_type"] = "INTRADAY"
                elif pt in ("C", "DELIVERY"):
                    r["product_type"] = "CNC"
                elif pt in ("M", "CARRYFORWARD", "NORMAL"):
                    r["product_type"] = "NRML"
                elif pt == "B":
                    r["product_type"] = "BO"
                elif pt == "H":
                    r["product_type"] = "CO"

                exch = str(r.get("exchange", "")).strip().upper()
                if "BFO" in exch:
                    r["exchange"] = "BFO"
                elif "BSE" in exch:
                    r["exchange"] = "BSE"
                elif exch in ("NSE", "NFO", "MCX", "CDS", "BCD"):
                    r["exchange"] = exch
                else:
                    r["exchange"] = "BSE"
            
    return web.json_response({"success": True, "data": result})


def get_credentials_from_static_csv(client_id: str):
    """Retrieves fresh dynamic credentials for a specific client_id (replaces legacy static CSV)."""
    from credentials.dynamic_loader import get_dynamic_credentials
    return get_dynamic_credentials(client_id)

@routes.get('/api/logs')
async def get_logs(request: web.Request):
    """Fetches logs for the Action Log UI."""
    await require_auth(request)
    log_type = request.query.get('type', 'system')
    
    if log_type in ('blocked', 'blocklist'):
        return web.json_response(get_blocked_users())
    
    loggers = {
        'auth': logger_auth,
        'audit': logger_audit,
        'api': logger_api,
        'orders': logger_exit,
        'system': logger_system,
        'monitor': logger_monitor,
        'health': logger_health
    }
    
    selected_logger = loggers.get(log_type, logger_system)
    history = selected_logger.get_history()
    
    return web.json_response([asdict(entry) for entry in history])

@routes.get('/api/audit-logs')
async def get_audit_logs(request: web.Request):
    """
    Returns structured login/logout audit trail from SQLite auth_audit_logs table.
    Supports limit, offset, action, username, status filter.
    """
    await require_auth(request)
    limit = int(request.query.get("limit", 100))
    offset = int(request.query.get("offset", 0))
    action_filter = request.query.get("action")
    status_filter = request.query.get("status")
    username_filter = request.query.get("username")
    
    query = "SELECT id, timestamp, username, action, status, ip_address, user_agent, details FROM auth_audit_logs WHERE 1=1"
    params = []
    
    if action_filter:
        query += " AND action = ?"
        params.append(action_filter)
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if username_filter:
        query += " AND username = ?"
        params.append(username_filter)
        
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    
    async with get_db() as db:
        async with db.execute(query, params) as cursor:
            columns = [desc[0] for desc in cursor.description]
            rows = await cursor.fetchall()
            logs = [dict(zip(columns, row)) for row in rows]
            
    return web.json_response({"success": True, "data": logs, "total": len(logs)})

@routes.get('/api/blocked-users')
async def list_blocked_users(request: web.Request):
    """Returns the list of all currently blocked credentials."""
    await require_auth(request)
    return web.json_response(get_blocked_users())

@routes.post('/api/blocked-users/retry')
async def retry_blocked_user(request: web.Request):
    """
    Fetches updated credentials from static CSV for the specified client_id,
    synchronizes them to SQLite, and attempts 1 single login attempt.
    """
    await require_auth(request)
    
    client_id = None
    try:
        data = await request.json()
        client_id = data.get("client_id")
    except Exception:
        pass
    if not client_id:
        client_id = request.query.get("client_id")
        
    if not client_id:
        return web.json_response({"success": False, "message": "Missing 'client_id' parameter."}, status=400)
        
    client_id = str(client_id).strip()
    username = get_client_username(client_id)
    
    # 0. Sync fresh credentials from remote source first so newest changes take effect
    try:
        from credentials.dynamic_loader import fetch_and_sync_credentials
        await fetch_and_sync_credentials()
    except Exception as e:
        logger.warning(f"Could not refresh credentials before single retry: {e}")

    # 1. Fetch fresh credentials from dynamic loader
    fresh_creds = get_credentials_from_static_csv(client_id)
    if not fresh_creds:
        return web.json_response({
            "success": False,
            "message": f"Credential record for {client_id} ({username}) not found in static CSV.",
            "blocked": True
        }, status=404)
        
    # 2. Update SQLite credentials table with fresh static values
    async with get_db() as db:
        if fresh_creds.get("token"):
            await db.execute("""
                UPDATE credentials 
                SET password = ?, api_key = ?, totp_secret = ?, static_ip = ?, status = 'working', token = ?, token_updated_at = datetime('now', 'localtime')
                WHERE client_id = ?
            """, (
                fresh_creds["password"],
                fresh_creds["api_key"],
                fresh_creds["totp_secret"],
                fresh_creds["static_ip"],
                fresh_creds["token"],
                client_id
            ))
        else:
            await db.execute("""
                UPDATE credentials 
                SET password = ?, api_key = ?, totp_secret = ?, static_ip = ?, status = 'working'
                WHERE client_id = ?
            """, (
                fresh_creds["password"],
                fresh_creds["api_key"],
                fresh_creds["totp_secret"],
                fresh_creds["static_ip"],
                client_id
            ))
        await db.commit()
        
        # Load the updated record
        db.row_factory = None
        async with db.execute(
            "SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE client_id = ?",
            (client_id,)
        ) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        return web.json_response({
            "success": False,
            "message": f"User record for {client_id} not found in database.",
            "blocked": True
        }, status=404)
        
    cred = Credential(
        id=row[0], broker=row[1], client_id=row[2], api_key=row[3], api_secret=row[4],
        totp_secret=row[5], password=row[6], static_ip=row[7],
        fingerprint=row[8], status=row[9], token=row[10], token_updated_at=row[11]
    )
    
    # 3. Attempt single-attempt login
    logger.info(f"🔄 [BLOCKLIST RETRY] Attempting single login for {cred.broker}:{cred.client_id} ({username}) with updated credentials from CSV")
    success = await refresh_token_with_backoff(cred)
    
    if success:
        return web.json_response({
            "success": True,
            "message": f"Successfully authenticated {username} ({client_id}). User removed from Block List and live polling resumed.",
            "blocked": False
        })
    else:
        blocked_info = get_blocked_user(client_id)
        reason = blocked_info.get("reason", "Authentication failed") if blocked_info else "Authentication failed"
        return web.json_response({
            "success": False,
            "message": f"Login failed for {username} ({client_id}): {reason}. Keeping user in Block List.",
            "blocked": True,
            "error": reason
        })


@routes.post('/api/blocked-users/sync-dropbox')
async def sync_dropbox_blocked_users(request: web.Request):
    """
    Downloads the latest credentials directly from Dropbox (or active source),
    synchronizes them to in-memory caches and SQLite, and automatically retries
    authentication for all currently blocked accounts with the updated credentials.
    Unblocks accounts that authenticate successfully and resumes live polling.
    """
    user = await require_auth(request)

    # 1. Download and synchronize fresh credentials from Dropbox / remote source
    synced_total = 0
    try:
        from credentials.dynamic_loader import fetch_and_sync_credentials
        synced_total = await fetch_and_sync_credentials()
        logger.info(f"🔄 [BLOCKLIST DROPBOX SYNC] Synced {synced_total} accounts from remote source for user={user.username}")
    except Exception as e:
        logger.error(f"❌ [BLOCKLIST DROPBOX SYNC] Failed to sync credentials from Dropbox: {e}")
        return web.json_response({
            "success": False,
            "message": f"Failed to download credentials from Dropbox: {e}",
            "synced_accounts": 0,
            "unblocked": [],
            "remaining_blocked": get_blocked_users()
        }, status=500)

    # 2. Retrieve all currently blocked accounts
    blocked_list = get_blocked_users()
    if not blocked_list:
        return web.json_response({
            "success": True,
            "message": f"Successfully synced {synced_total} accounts from Dropbox. No blocked accounts found.",
            "synced_accounts": synced_total,
            "unblocked": [],
            "remaining_blocked": []
        })

    # 3. Attempt re-authentication for each blocked user with fresh credentials
    unblocked_names = []
    still_blocked_names = []

    for item in blocked_list:
        client_id = item.get("client_id")
        if not client_id:
            continue
        client_id = str(client_id).strip()
        username = get_client_username(client_id)
        fresh_creds = get_credentials_from_static_csv(client_id)
        if not fresh_creds:
            still_blocked_names.append(f"{username} ({client_id}): not found in Dropbox CSV")
            continue

        # Update SQLite credentials table with fresh values
        async with get_db() as db:
            if fresh_creds.get("token"):
                await db.execute("""
                    UPDATE credentials 
                    SET password = ?, api_key = ?, totp_secret = ?, static_ip = ?, status = 'working', token = ?, token_updated_at = datetime('now', 'localtime')
                    WHERE client_id = ?
                """, (
                    fresh_creds["password"],
                    fresh_creds["api_key"],
                    fresh_creds["totp_secret"],
                    fresh_creds["static_ip"],
                    fresh_creds["token"],
                    client_id
                ))
            else:
                await db.execute("""
                    UPDATE credentials 
                    SET password = ?, api_key = ?, totp_secret = ?, static_ip = ?, status = 'working'
                    WHERE client_id = ?
                """, (
                    fresh_creds["password"],
                    fresh_creds["api_key"],
                    fresh_creds["totp_secret"],
                    fresh_creds["static_ip"],
                    client_id
                ))
            await db.commit()

            # Load the updated record
            db.row_factory = None
            async with db.execute(
                "SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE client_id = ?",
                (client_id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            still_blocked_names.append(f"{username} ({client_id}): missing in DB")
            continue

        cred = Credential(
            id=row[0], broker=row[1], client_id=row[2], api_key=row[3], api_secret=row[4],
            totp_secret=row[5], password=row[6], static_ip=row[7],
            fingerprint=row[8], status=row[9], token=row[10], token_updated_at=row[11]
        )

        logger.info(f"🔄 [BLOCKLIST DROPBOX SYNC] Retrying {cred.broker}:{cred.client_id} ({username}) with fresh Dropbox credentials")
        success = await refresh_token_with_backoff(cred)
        if success:
            unblocked_names.append(f"{username} ({client_id})")
        else:
            blocked_info = get_blocked_user(client_id)
            err = blocked_info.get("reason", "Auth failed") if blocked_info else "Auth failed"
            still_blocked_names.append(f"{username} ({client_id}): {err}")

    # 4. Construct user message
    unblocked_count = len(unblocked_names)
    still_count = len(still_blocked_names)

    if unblocked_count > 0 and still_count == 0:
        summary_msg = f"Synced {synced_total} accounts from Dropbox. Successfully unblocked all {unblocked_count} account(s): {', '.join(unblocked_names)}."
    elif unblocked_count > 0 and still_count > 0:
        summary_msg = f"Synced {synced_total} accounts from Dropbox. Unblocked {unblocked_count} account(s) ({', '.join(unblocked_names)}). {still_count} account(s) still failing."
    else:
        summary_msg = f"Synced {synced_total} accounts from Dropbox. All {still_count} blocked account(s) failed authentication. Please verify credentials in Dropbox."

    return web.json_response({
        "success": True,
        "message": summary_msg,
        "synced_accounts": synced_total,
        "unblocked": unblocked_names,
        "still_blocked": still_blocked_names,
        "remaining_blocked": get_blocked_users()
    })

