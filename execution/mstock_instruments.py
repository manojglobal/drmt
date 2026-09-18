import logging
from typing import Optional
from core.engine import core_engine
from models.schemas import Credential
from credentials.httpclient import proxy_request

logger = logging.getLogger("DRMT.MStockInstruments")

REDIS_KEY_TOKEN_MAP = "mstock:token_to_tsym"
REDIS_KEY_SYMBOL_MAP = "mstock:symbol_to_tsym"
REDIS_CACHE_TTL = 86400  # 24 hours

_is_loading = False

async def ensure_mstock_instruments_cached(cred: Credential) -> bool:
    """
    Fetches MStock OpenAPIScripMaster and caches the token/symbol -> tradingsymbol mapping in Redis.
    If Redis already has data, returns True immediately without making network calls.
    """
    global _is_loading
    redis = core_engine.redis_pool
    if not redis:
        return False

    try:
        count = await redis.hlen(REDIS_KEY_TOKEN_MAP)
        if count > 1000:
            return True
    except Exception as e:
        logger.warning(f"Failed to check Redis for MStock instruments: {e}")

    if _is_loading:
        return False
    _is_loading = True

    try:
        url = "https://api.mstock.trade/openapi/typeb/instruments/OpenAPIScripMaster"
        headers = {
            "Authorization": f"Bearer {cred.token}",
            "X-PrivateKey": cred.api_key,
            "X-Mirae-Version": "1"
        }
        logger.info("📥 Fetching MStock OpenAPIScripMaster to populate Redis...")
        data = await proxy_request("GET", url, str(cred.static_ip), headers=headers)
        
        if not isinstance(data, list):
            logger.warning(f"Unexpected ScripMaster response format: {type(data)}")
            return False

        token_map = {}
        symbol_map = {}
        
        for item in data:
            token = item.get("token")
            name = item.get("name")
            exch = item.get("exch_seg")
            symbol = item.get("symbol")
            itype = (item.get("instrumenttype") or "").upper()
            is_eq = itype in ("EQ", "EQUITY")

            if token and name and exch:
                key = f"{exch}:{token}"
                # Prioritize EQ over non-EQ for overlapping tokens on the same exchange
                if key not in token_map or is_eq:
                    token_map[key] = name

            if symbol and name and exch:
                key = f"{exch}:{symbol}"
                if key not in symbol_map or is_eq:
                    symbol_map[key] = name

        if token_map:
            # Batch write to Redis
            await redis.hset(REDIS_KEY_TOKEN_MAP, mapping=token_map)
            await redis.expire(REDIS_KEY_TOKEN_MAP, REDIS_CACHE_TTL)
            
        if symbol_map:
            await redis.hset(REDIS_KEY_SYMBOL_MAP, mapping=symbol_map)
            await redis.expire(REDIS_KEY_SYMBOL_MAP, REDIS_CACHE_TTL)

        logger.info(f"✅ Cached {len(token_map)} tokens and {len(symbol_map)} symbols from MStock Scrip Master in Redis.")
        return True
    except Exception as e:
        logger.error(f"Failed to load MStock Scrip Master into Redis: {e}")
        return False
    finally:
        _is_loading = False


async def get_mstock_trading_symbol(symbol: str, token: str, exchange: str) -> Optional[str]:
    """
    Looks up official exchange trading symbol from Redis Script Master mapping.
    1. Primary: exchange:token
    2. Fallback: exchange:symbol
    """
    redis = core_engine.redis_pool
    if not redis:
        return None

    try:
        if token and exchange:
            tsym = await redis.hget(REDIS_KEY_TOKEN_MAP, f"{exchange}:{token}")
            if tsym:
                return tsym

        if symbol and exchange:
            tsym = await redis.hget(REDIS_KEY_SYMBOL_MAP, f"{exchange}:{symbol}")
            if tsym:
                return tsym
    except Exception as e:
        logger.warning(f"Error reading MStock symbol mapping from Redis: {e}")

    return None
