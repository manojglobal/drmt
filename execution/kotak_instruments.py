"""
execution/kotak_instruments.py — Kotak Neo Instrument Master & Symbol Mapping Module for DRMT V2.
Handles:
1. Fast O(1) resolution of exchange tokens <-> trading symbols for Kotak Neo Trade API.
2. Segment formatting (nse_cm, bse_cm, nse_fo, bse_fo, mcx_fo).
3. In-memory and Redis dual caching with 24-hour TTL.
4. Fallback seed master for liquid index and equity instruments.
"""

import asyncio
import json
import logging
from typing import Optional, Dict, Any, List

from core.engine import core_engine
from models.schemas import Credential

logger = logging.getLogger("DRMT.KotakInstruments")

REDIS_KEY_KOTAK_TOKEN_MAP = "kotak:token_to_tsym"
REDIS_KEY_KOTAK_SYMBOL_MAP = "kotak:symbol_to_token"
REDIS_CACHE_TTL = 86400  # 24 hours

# In-memory fast lookup caches
_kotak_token_to_tsym: Dict[str, str] = {}
_kotak_symbol_to_token: Dict[str, str] = {}
_is_loading = False

# High-liquidity seed mapping for common instruments traded on Kotak Neo
SEED_INSTRUMENTS: List[Dict[str, str]] = [
    {"token": "3787", "symbol": "WIPRO-EQ", "base_symbol": "WIPRO", "exchange_seg": "nse_cm"},
    {"token": "2885", "symbol": "RELIANCE-EQ", "base_symbol": "RELIANCE", "exchange_seg": "nse_cm"},
    {"token": "11536", "symbol": "TCS-EQ", "base_symbol": "TCS", "exchange_seg": "nse_cm"},
    {"token": "1594", "symbol": "INFY-EQ", "base_symbol": "INFY", "exchange_seg": "nse_cm"},
    {"token": "1333", "symbol": "HDFCBANK-EQ", "base_symbol": "HDFCBANK", "exchange_seg": "nse_cm"},
    {"token": "4963", "symbol": "ICICIBANK-EQ", "base_symbol": "ICICIBANK", "exchange_seg": "nse_cm"},
    {"token": "3045", "symbol": "SBIN-EQ", "base_symbol": "SBIN", "exchange_seg": "nse_cm"},
    {"token": "1922", "symbol": "KOTAKBANK-EQ", "base_symbol": "KOTAKBANK", "exchange_seg": "nse_cm"},
    {"token": "526", "symbol": "AXISBANK-EQ", "base_symbol": "AXISBANK", "exchange_seg": "nse_cm"},
    {"token": "11630", "symbol": "NTPC-EQ", "base_symbol": "NTPC", "exchange_seg": "nse_cm"},
    {"token": "17963", "symbol": "NESTLEIND-EQ", "base_symbol": "NESTLEIND", "exchange_seg": "nse_cm"},
    {"token": "10940", "symbol": "TATAMOTORS-EQ", "base_symbol": "TATAMOTORS", "exchange_seg": "nse_cm"},
    {"token": "3499", "symbol": "TATASTEEL-EQ", "base_symbol": "TATASTEEL", "exchange_seg": "nse_cm"},
    {"token": "317", "symbol": "BAJFINANCE-EQ", "base_symbol": "BAJFINANCE", "exchange_seg": "nse_cm"},
    {"token": "1660", "symbol": "ITC-EQ", "base_symbol": "ITC", "exchange_seg": "nse_cm"},
    {"token": "21808", "symbol": "NIFTY", "base_symbol": "NIFTY", "exchange_seg": "nse_fo"},
    {"token": "26009", "symbol": "BANKNIFTY", "base_symbol": "BANKNIFTY", "exchange_seg": "nse_fo"},
    {"token": "26037", "symbol": "FINNIFTY", "base_symbol": "FINNIFTY", "exchange_seg": "nse_fo"},
]

# Pre-populate in-memory seed maps immediately
for _item in SEED_INSTRUMENTS:
    _tok = _item["token"]
    _sym = _item["symbol"]
    _base = _item["base_symbol"]
    _seg = _item["exchange_seg"]
    _kotak_token_to_tsym[f"{_seg}|{_tok}"] = _sym
    _kotak_token_to_tsym[_tok] = _sym
    _kotak_symbol_to_token[f"{_seg}:{_sym}"] = _tok
    _kotak_symbol_to_token[f"{_seg}:{_base}"] = _tok
    _kotak_symbol_to_token[_sym] = _tok
    _kotak_symbol_to_token[_base] = _tok


def resolve_kotak_exchange_segment(exchange: Optional[str], symbol: Optional[str] = "") -> str:
    """
    Normalizes generic exchange names to Kotak Neo Trade API format:
    - nse_cm: NSE Cash / Equity
    - bse_cm: BSE Cash / Equity
    - nse_fo: NSE Derivatives / Options
    - bse_fo: BSE Derivatives / Options
    - mcx_fo: MCX Commodity Derivatives
    """
    exch = str(exchange or "").upper().strip()
    sym = str(symbol or "").upper().strip()
    is_derivative = exch in ("NFO", "BFO", "NSE_FO", "BSE_FO") or any(
        x in sym for x in ("-CE", "-PE", " CE", " PE", " FUT", "-FUT")
    )

    if "MCX" in exch:
        return "mcx_fo"
    if "BSE" in exch or "BFO" in exch:
        return "bse_fo" if is_derivative else "bse_cm"
    return "nse_fo" if is_derivative else "nse_cm"


async def ensure_kotak_instruments_cached(cred: Optional[Credential] = None) -> bool:
    """
    Ensures Kotak Neo instrument cache is initialized in memory and Redis.
    Populates fast lookups for exchange token <-> trading symbol.
    """
    global _is_loading, _kotak_token_to_tsym, _kotak_symbol_to_token

    # 1. Fast in-memory check
    if len(_kotak_token_to_tsym) > 0:
        return True

    redis = core_engine.redis_pool
    if redis:
        try:
            cached_tokens = await redis.hgetall(REDIS_KEY_KOTAK_TOKEN_MAP)
            if cached_tokens and len(cached_tokens) > 0:
                _kotak_token_to_tsym = {
                    (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
                    for k, v in cached_tokens.items()
                }
                cached_symbols = await redis.hgetall(REDIS_KEY_KOTAK_SYMBOL_MAP)
                _kotak_symbol_to_token = {
                    (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
                    for k, v in cached_symbols.items()
                }
                logger.info(f"✅ Loaded {len(_kotak_token_to_tsym)} Kotak Neo instruments from Redis.")
                return True
        except Exception as e:
            logger.warning(f"Could not read Kotak Neo instruments from Redis: {e}")

    if _is_loading:
        return False
    _is_loading = True

    try:
        token_map: Dict[str, str] = {}
        symbol_map: Dict[str, str] = {}

        # Populate from high-liquidity seed master
        for item in SEED_INSTRUMENTS:
            tok = item["token"]
            sym = item["symbol"]
            base = item["base_symbol"]
            seg = item["exchange_seg"]

            # Key by seg|token and token alone
            token_map[f"{seg}|{tok}"] = sym
            token_map[tok] = sym

            # Key by seg:symbol, base_symbol, and symbol
            symbol_map[f"{seg}:{sym}"] = tok
            symbol_map[f"{seg}:{base}"] = tok
            symbol_map[sym] = tok
            symbol_map[base] = tok

        _kotak_token_to_tsym = token_map
        _kotak_symbol_to_token = symbol_map

        # Write to Redis if available
        if redis and token_map:
            await redis.hset(REDIS_KEY_KOTAK_TOKEN_MAP, mapping=token_map)
            await redis.expire(REDIS_KEY_KOTAK_TOKEN_MAP, REDIS_CACHE_TTL)
            await redis.hset(REDIS_KEY_KOTAK_SYMBOL_MAP, mapping=symbol_map)
            await redis.expire(REDIS_KEY_KOTAK_SYMBOL_MAP, REDIS_CACHE_TTL)

        logger.info(f"✅ Cached {len(token_map)} Kotak Neo instrument lookups.")
        return True
    except Exception as e:
        logger.error(f"Failed to load Kotak Neo instrument master: {e}")
        return False
    finally:
        _is_loading = False


async def get_kotak_exchange_token(symbol: str, exchange: str = "") -> Optional[str]:
    """
    Resolves symbol (e.g. 'WIPRO' or 'WIPRO-EQ') to Kotak numeric exchange token (e.g. '3787').
    Checks in-memory cache first, then Redis.
    """
    if not symbol:
        return None

    sym_clean = str(symbol).strip().upper()
    seg = resolve_kotak_exchange_segment(exchange, sym_clean)

    # 1. In-memory check
    tok = _kotak_symbol_to_token.get(f"{seg}:{sym_clean}") or _kotak_symbol_to_token.get(sym_clean)
    if tok:
        return tok

    # Strip -EQ suffix if present
    base_sym = sym_clean.replace("-EQ", "")
    tok = _kotak_symbol_to_token.get(f"{seg}:{base_sym}") or _kotak_symbol_to_token.get(base_sym)
    if tok:
        return tok

    # 2. Redis check
    redis = core_engine.redis_pool
    if redis:
        try:
            val = await redis.hget(REDIS_KEY_KOTAK_SYMBOL_MAP, f"{seg}:{sym_clean}")
            if not val:
                val = await redis.hget(REDIS_KEY_KOTAK_SYMBOL_MAP, sym_clean)
            if not val:
                val = await redis.hget(REDIS_KEY_KOTAK_SYMBOL_MAP, base_sym)
            if val:
                res = val.decode() if isinstance(val, bytes) else str(val)
                _kotak_symbol_to_token[sym_clean] = res
                return res
        except Exception:
            pass

    return None


async def get_kotak_trading_symbol(token: str, exchange: str = "") -> Optional[str]:
    """
    Resolves numeric token (e.g. '3787') to exchange trading symbol (e.g. 'WIPRO-EQ').
    """
    if not token:
        return None

    t_str = str(token).strip()
    seg = resolve_kotak_exchange_segment(exchange)

    sym = _kotak_token_to_tsym.get(f"{seg}|{t_str}") or _kotak_token_to_tsym.get(t_str)
    if sym:
        return sym

    redis = core_engine.redis_pool
    if redis:
        try:
            val = await redis.hget(REDIS_KEY_KOTAK_TOKEN_MAP, f"{seg}|{t_str}")
            if not val:
                val = await redis.hget(REDIS_KEY_KOTAK_TOKEN_MAP, t_str)
            if val:
                res = val.decode() if isinstance(val, bytes) else str(val)
                _kotak_token_to_tsym[t_str] = res
                return res
        except Exception:
            pass

    return None
