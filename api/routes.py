import logging
from aiohttp import web
from api.auth import require_auth
from database.db import get_db
from models.schemas import Credential, Position
from position.chronos import refresh_token_with_backoff
from execution.engine import mass_exit
from utils.user_mapping import get_client_username, get_client_ids_for_user

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

@routes.post('/api/credentials/refresh')
async def refresh_credentials(request: web.Request):
    """
    Credential Refresh Button: Forces token refresh without server restart.
    """
    user = await require_auth(request)
    
    success_count = 0
    async with get_db() as db:
        db.row_factory = None
        async with db.execute("SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE status = 'working' AND user_id = ?", (user.id,)) as cursor:
            rows = await cursor.fetchall()
            
    for row in rows:
        cred = Credential(
            id=row[0], broker=row[1], client_id=row[2], api_key=row[3], api_secret=row[4],
            totp_secret=row[5], password=row[6], static_ip=row[7],
            fingerprint=row[8], status=row[9], token=row[10], token_updated_at=row[11]
        )
        if await refresh_token_with_backoff(cred):
            success_count += 1
            
    return web.json_response({"success": True, "refreshed": success_count, "total": len(rows)})

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
        async with db.execute("SELECT id, broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at FROM credentials WHERE status = 'working' AND user_id = ?", (user.id,)) as cursor:
            rows = await cursor.fetchall()
            
    credentials = [Credential(
        id=r[0], broker=r[1], client_id=r[2], api_key=r[3], api_secret=r[4],
        totp_secret=r[5], password=r[6], static_ip=r[7],
        fingerprint=r[8], status=r[9], token=r[10], token_updated_at=r[11]
    ) for r in rows]
    
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
            "broker": r["broker"],
            "symbol": r["symbol"],
            "success": r["success"],
            "message": r["message"]
        }
        for r in results
    ]
    return web.json_response({
        "success": success_count > 0 or len(positions) == 0,
        "message": f"Exited {success_count}/{len(positions)} positions",
        "results": client_results
    })

@routes.get('/api/exited')
async def get_exited_positions(request: web.Request):
    """
    Exited filter rule: If broker or client is "ALL", skip SQL filter.
    """
    await require_auth(request)
    
    broker = request.query.get("broker", "ALL")
    client_id = request.query.get("client_id", "ALL")
    
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
        
    query += " ORDER BY exited_at DESC"
    
    async with get_db() as db:
        db.row_factory = None # For simplicity, returning raw dicts in production we'd use aiosqlite.Row
        async with db.execute(query, params) as cursor:
            columns = [desc[0] for desc in cursor.description]
            rows = await cursor.fetchall()
            result = [dict(zip(columns, row)) for row in rows]
            for r in result:
                r["username"] = get_client_username(r.get("client_id", ""))
            
    return web.json_response({"success": True, "data": result})


from logger.logger import logger_auth, logger_exit, logger_system, logger_monitor, logger_health
from dataclasses import asdict

@routes.get('/api/logs')
async def get_logs(request: web.Request):
    """Fetches logs for the Action Log UI."""
    await require_auth(request)
    log_type = request.query.get('type', 'system')
    
    loggers = {
        'auth': logger_auth,
        'orders': logger_exit,
        'system': logger_system,
        'monitor': logger_monitor,
        'health': logger_health
    }
    
    selected_logger = loggers.get(log_type, logger_system)
    history = selected_logger.get_history()
    
    return web.json_response([asdict(entry) for entry in history])

