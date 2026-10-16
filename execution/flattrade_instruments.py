"""
execution/flattrade_instruments.py — Flattrade Symbol Master & Segment Resolver for DRMT V2.
Handles:
1. Fast O(1) resolution of exchange tokens <-> trading symbols for Flattrade (Shoonya API).
2. Segment formatting and routing (BSE vs BFO, NSE vs NFO, MCX).
3. In-memory and Redis dual caching with 24-hour TTL.
4. Fallback seed contracts for liquid index and equity instruments.
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List

from core.engine import core_engine
from models.schemas import Credential

logger = logging.getLogger("DRMT.FlattradeInstruments")

REDIS_KEY_FLATTRADE_TOKEN_MAP = "flattrade:token_to_tsym"
REDIS_KEY_FLATTRADE_SYMBOL_MAP = "flattrade:symbol_to_token"
REDIS_CACHE_TTL = 86400  # 24 hours

_flattrade_token_to_tsym: Dict[str, str] = {}
_flattrade_symbol_to_token: Dict[str, str] = {}
_is_loading = False

SEED_INSTRUMENTS: List[Dict[str, str]] = [
    {"token": "3787", "symbol": "WIPRO-EQ", "base_symbol": "WIPRO", "exchange": "NSE"},
    {"token": "2885", "symbol": "RELIANCE-EQ", "base_symbol": "RELIANCE", "exchange": "NSE"},
    {"token": "11536", "symbol": "TCS-EQ", "base_symbol": "TCS", "exchange": "NSE"},
    {"token": "1594", "symbol": "INFY-EQ", "base_symbol": "INFY", "exchange": "NSE"},
    {"token": "1333", "symbol": "HDFCBANK-EQ", "base_symbol": "HDFCBANK", "exchange": "NSE"},
    {"token": "4963", "symbol": "ICICIBANK-EQ", "base_symbol": "ICICIBANK", "exchange": "NSE"},
    {"token": "3045", "symbol": "SBIN-EQ", "base_symbol": "SBIN", "exchange": "NSE"},
    {"token": "1922", "symbol": "KOTAKBANK-EQ", "base_symbol": "KOTAKBANK", "exchange": "NSE"},
    {"token": "526", "symbol": "AXISBANK-EQ", "base_symbol": "AXISBANK", "exchange": "NSE"},
    {"token": "26000", "symbol": "NIFTY", "base_symbol": "NIFTY", "exchange": "NFO"},
    {"token": "26009", "symbol": "BANKNIFTY", "base_symbol": "BANKNIFTY", "exchange": "NFO"},
]

# Pre-populate in-memory seed maps immediately
for _item in SEED_INSTRUMENTS:
    _tok = _item["token"]
    _sym = _item["symbol"]
    _base = _item["base_symbol"]
    _exch = _item["exchange"]
    _flattrade_token_to_tsym[f"{_exch}:{_tok}"] = _sym
    _flattrade_token_to_tsym[_tok] = _sym
    _flattrade_symbol_to_token[f"{_exch}:{_sym}"] = _tok
    _flattrade_symbol_to_token[f"{_exch}:{_base}"] = _tok
    _flattrade_symbol_to_token[_sym] = _tok
    _flattrade_symbol_to_token[_base] = _tok


def resolve_flattrade_exchange(exchange: Optional[str], symbol: Optional[str] = "") -> str:
    """
    Normalizes exchange identifiers for Flattrade Shoonya API:
    Routes derivatives to BFO/NFO and equities to BSE/NSE.
    """
    exch = str(exchange or "").upper().strip()
    sym = str(symbol or "").upper().strip()
    is_derivative = exch in ("NFO", "BFO", "NSE_FO", "BSE_FO") or any(
        x in sym for x in ("-CE", "-PE", " CE", " PE", " FUT", "-FUT", "SENSEX", "BANKEX")
    )

    if "MCX" in exch:
        return "MCX"
    if "BSE" in exch or "BFO" in exch:
        return "BFO" if is_derivative else "BSE"
    if "NSE" in exch or "NFO" in exch:
        return "NFO" if is_derivative else "NSE"
    return exch or "NSE"


async def ensure_flattrade_instruments_cached(cred: Optional[Credential] = None) -> bool:
    """Ensures Flattrade symbol cache is initialized in memory and Redis."""
    global _is_loading, _flattrade_token_to_tsym, _flattrade_symbol_to_token

    if len(_flattrade_token_to_tsym) > 0:
        return True

    redis = core_engine.redis_pool
    if redis:
        try:
            cached_tokens = await redis.hgetall(REDIS_KEY_FLATTRADE_TOKEN_MAP)
            if cached_tokens and len(cached_tokens) > 0:
                _flattrade_token_to_tsym = {
                    (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
                    for k, v in cached_tokens.items()
                }
                cached_symbols = await redis.hgetall(REDIS_KEY_FLATTRADE_SYMBOL_MAP)
                _flattrade_symbol_to_token = {
                    (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
                    for k, v in cached_symbols.items()
                }
                logger.info(f"✅ Loaded {len(_flattrade_token_to_tsym)} Flattrade instruments from Redis.")
                return True
        except Exception as e:
            logger.warning(f"Could not read Flattrade instruments from Redis: {e}")

    if _is_loading:
        return False
    _is_loading = True

    try:
        token_map: Dict[str, str] = {}
        symbol_map: Dict[str, str] = {}

        for item in SEED_INSTRUMENTS:
            tok = item["token"]
            sym = item["symbol"]
            base = item["base_symbol"]
            exch = item["exchange"]

            token_map[f"{exch}:{tok}"] = sym
            token_map[tok] = sym

            symbol_map[f"{exch}:{sym}"] = tok
            symbol_map[f"{exch}:{base}"] = tok
            symbol_map[sym] = tok
            symbol_map[base] = tok

        _flattrade_token_to_tsym = token_map
        _flattrade_symbol_to_token = symbol_map

        if redis and token_map:
            await redis.hset(REDIS_KEY_FLATTRADE_TOKEN_MAP, mapping=token_map)
            await redis.expire(REDIS_KEY_FLATTRADE_TOKEN_MAP, REDIS_CACHE_TTL)
            await redis.hset(REDIS_KEY_FLATTRADE_SYMBOL_MAP, mapping=symbol_map)
            await redis.expire(REDIS_KEY_FLATTRADE_SYMBOL_MAP, REDIS_CACHE_TTL)

        logger.info(f"✅ Cached {len(token_map)} Flattrade instruments.")
        return True
    except Exception as e:
        logger.error(f"Failed to load Flattrade instrument master: {e}")
        return False
    finally:
        _is_loading = False


async def get_flattrade_token(symbol: str, exchange: str = "") -> Optional[str]:
    """Resolves symbol to Flattrade exchange token."""
    if not symbol:
        return None

    sym_clean = str(symbol).strip().upper()
    exch = resolve_flattrade_exchange(exchange, sym_clean)

    tok = _flattrade_symbol_to_token.get(f"{exch}:{sym_clean}") or _flattrade_symbol_to_token.get(sym_clean)
    if tok:
        return tok

    base_sym = sym_clean.replace("-EQ", "")
    return _flattrade_symbol_to_token.get(f"{exch}:{base_sym}") or _flattrade_symbol_to_token.get(base_sym)


async def get_flattrade_trading_symbol(token: str, exchange: str = "") -> Optional[str]:
    """Resolves token to trading symbol."""
    if not token:
        return None

    t_str = str(token).strip()
    exch = resolve_flattrade_exchange(exchange)

    return _flattrade_token_to_tsym.get(f"{exch}:{t_str}") or _flattrade_token_to_tsym.get(t_str)
