"""
execution/axis_instruments.py — Axis Direct Instrument Master & Symbol Mapping Module for DRMT V2.
Handles:
1. Loading Axis Direct Security Master (SecurityMaster.csv) and caching in Redis & Memory.
2. Fast O(1) resolution of Axis numeric scriptId -> exchange trading symbol (e.g. 223787 -> WIPRO).
3. Fallback close price caching to safeguard against 0.00 LTP when live market quotes are unavailable.
4. Live market LTP quotes via Axis Direct /feedbroadcast/multi-touch-line endpoint.
"""

import asyncio
import csv
import io
import json
import logging
import os
from typing import Optional, Dict, Any, List

from core.engine import core_engine
from models.schemas import Credential
from credentials.axisdirect import (
    AXIS_INVEST_BASE_URL,
    get_axis_public_key,
    send_axis_api_request,
)

logger = logging.getLogger("DRMT.AxisInstruments")

AXIS_SECURITY_MASTER_URL = "https://invest-static-assets.axisdirect.in/TELESCOPE/security_master_web/SecurityMaster.csv"
LOCAL_AXIS_MASTER_PATH = "static/axis_security_master.json"

REDIS_KEY_AXIS_TOKEN_MAP = "axis:token_to_tsym"
REDIS_KEY_AXIS_PRICE_MAP = "axis:token_to_price"
REDIS_CACHE_TTL = 86400  # 24 hours

# In-memory fast lookup caches
_axis_token_to_tsym: Dict[str, str] = {}
_axis_token_to_price: Dict[str, float] = {}
_axis_symbol_to_token: Dict[str, str] = {}
_is_loading = False


def load_local_axis_instruments() -> bool:
    """Loads symbols from local static/axis_security_master.json if present."""
    global _axis_token_to_tsym, _axis_token_to_price, _axis_symbol_to_token
    if _axis_token_to_tsym:
        return True

    if os.path.exists(LOCAL_AXIS_MASTER_PATH):
        try:
            with open(LOCAL_AXIS_MASTER_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _axis_token_to_tsym = data.get("tokens", {})
            _axis_token_to_price = {k: float(v) for k, v in data.get("prices", {}).items()}
            exch_map = data.get("exchanges", {})
            _axis_symbol_to_token = {}
            for sid, sym in _axis_token_to_tsym.items():
                s_upper = sym.upper()
                exch = str(exch_map.get(sid) or "").upper()
                if exch:
                    _axis_symbol_to_token[f"{exch}:{s_upper}"] = sid
                if s_upper not in _axis_symbol_to_token or exch == "NSE":
                    _axis_symbol_to_token[s_upper] = sid

            logger.info(f"✅ Loaded {len(_axis_token_to_tsym)} Axis Direct instruments from local cache.")
            return True
        except Exception as e:
            logger.warning(f"Could not load local Axis security master: {e}")
    return False


async def ensure_axis_instruments_cached(cred: Optional[Credential] = None) -> bool:
    """
    Ensures Axis Direct instruments are cached in Redis and memory.
    1. Fast in-memory cache check.
    2. Local static/axis_security_master.json file check.
    3. Redis check & hydration.
    4. Downloads SecurityMaster.csv from Axis Direct CDN if cache is missing.
    """
    global _is_loading, _axis_token_to_tsym, _axis_token_to_price

    # 1. Fast in-memory check
    if len(_axis_token_to_tsym) > 1000:
        return True

    # 2. Local JSON file check
    load_local_axis_instruments()
    if len(_axis_token_to_tsym) > 1000:
        redis = core_engine.redis_pool
        if redis:
            try:
                count = await redis.hlen(REDIS_KEY_AXIS_TOKEN_MAP)
                if count < 1000:
                    chunk: Dict[str, str] = {}
                    for k, v in _axis_token_to_tsym.items():
                        chunk[k] = v
                        if len(chunk) >= 10000:
                            await redis.hset(REDIS_KEY_AXIS_TOKEN_MAP, mapping=chunk)
                            chunk = {}
                    if chunk:
                        await redis.hset(REDIS_KEY_AXIS_TOKEN_MAP, mapping=chunk)
                    await redis.expire(REDIS_KEY_AXIS_TOKEN_MAP, REDIS_CACHE_TTL)
            except Exception as e:
                logger.warning(f"Redis write warning for Axis instruments: {e}")
        return True

    redis = core_engine.redis_pool
    if redis:
        try:
            count = await redis.hlen(REDIS_KEY_AXIS_TOKEN_MAP)
            if count > 1000:
                return True
        except Exception as e:
            logger.warning(f"Failed to check Redis for Axis instruments: {e}")

    if _is_loading:
        return False
    _is_loading = True

    try:
        session = core_engine.http_session
        if not session:
            logger.warning("HTTP session not initialized; skipping Axis SecurityMaster download.")
            return False

        logger.info("📥 Downloading Axis Direct SecurityMaster.csv in background...")
        async with session.get(AXIS_SECURITY_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                logger.error(f"Axis SecurityMaster download failed with status {resp.status}")
                return False
            raw_text = await resp.text(encoding="utf-8", errors="ignore")

        reader = csv.DictReader(io.StringIO(raw_text))
        token_map: Dict[str, str] = {}
        price_map: Dict[str, float] = {}

        for row in reader:
            sid = row.get("scriptid")
            if not sid:
                continue
            seg = str(row.get("segment") or "").strip().upper()
            sym = (row.get("coname") if seg == "FO" else row.get("exchange_symbol")) or row.get("exchange_symbol") or row.get("coname") or ""
            if sym:
                token_map[sid] = sym.strip()
            cp = row.get("closeprice")
            if cp:
                try:
                    v = float(cp)
                    if v > 0:
                        price_map[sid] = v
                except ValueError:
                    pass

        _axis_token_to_tsym = token_map
        _axis_token_to_price = price_map

        # Write to local cache file
        try:
            with open(LOCAL_AXIS_MASTER_PATH, "w", encoding="utf-8") as f:
                json.dump({"tokens": token_map, "prices": price_map}, f)
        except Exception as e:
            logger.warning(f"Failed to write local Axis Security Master JSON: {e}")

        # Hydrate Redis in chunks
        if redis and token_map:
            try:
                chunk = {}
                for k, v in token_map.items():
                    chunk[k] = v
                    if len(chunk) >= 10000:
                        await redis.hset(REDIS_KEY_AXIS_TOKEN_MAP, mapping=chunk)
                        chunk = {}
                if chunk:
                    await redis.hset(REDIS_KEY_AXIS_TOKEN_MAP, mapping=chunk)
                await redis.expire(REDIS_KEY_AXIS_TOKEN_MAP, REDIS_CACHE_TTL)
            except Exception as e:
                logger.warning(f"Failed to populate Redis for Axis instruments: {e}")

        logger.info(f"✅ Cached {len(token_map)} Axis Direct instruments into Redis and local cache.")
        return True
    except Exception as e:
        logger.error(f"Failed to load Axis Direct Security Master: {e}")
        return False
    finally:
        _is_loading = False


async def get_axis_trading_symbol(script_id: str, exchange: str = "") -> Optional[str]:
    """
    Looks up official exchange trading symbol for Axis script_id.
    1. In-memory cache
    2. Redis hash axis:token_to_tsym
    3. Local cache file
    """
    sid = str(script_id or "").strip()
    if not sid:
        return None

    # 1. In-memory lookup
    if sid in _axis_token_to_tsym:
        return _axis_token_to_tsym[sid]

    # 2. Local cache check
    if not _axis_token_to_tsym:
        load_local_axis_instruments()
        if sid in _axis_token_to_tsym:
            return _axis_token_to_tsym[sid]

    # 3. Redis lookup
    redis = core_engine.redis_pool
    if redis:
        try:
            val = await redis.hget(REDIS_KEY_AXIS_TOKEN_MAP, sid)
            if val:
                sym = val.decode("utf-8") if isinstance(val, bytes) else str(val)
                _axis_token_to_tsym[sid] = sym
                return sym
        except Exception:
            pass

    return None


def get_axis_script_id(symbol: str, exchange: str = "") -> Optional[str]:
    """Reverse lookup: tradingSymbol -> scriptId (handles exchange prefix e.g. NSE:WIPRO)."""
    global _axis_symbol_to_token
    sym = str(symbol or "").strip().upper()
    exch = str(exchange or "").strip().upper()
    if not sym:
        return None
    if not _axis_symbol_to_token:
        load_local_axis_instruments()
    if exch:
        res = _axis_symbol_to_token.get(f"{exch}:{sym}")
        if res:
            return res
    return _axis_symbol_to_token.get(sym)


def get_axis_close_price(script_id: str) -> float:
    """Returns the cached previous close price for a scriptId as defensive LTP fallback."""
    sid = str(script_id or "").strip()
    if not _axis_token_to_price:
        load_local_axis_instruments()
    return _axis_token_to_price.get(sid, 0.0)


async def fetch_axis_quotes(script_ids: List[str], cred: Credential) -> Dict[str, float]:
    """
    Fetches real-time market LTP quotes from Axis Direct via /feedbroadcast/multi-touch-line.
    Returns {script_id: ltp}.
    """
    if not script_ids:
        return {}

    api_client_id = cred.api_key or ""
    authorization_key = cred.api_secret or ""
    sub_account_id = getattr(cred, "sub_account_id", "") or cred.client_id or ""
    access_token = cred.token or ""
    public_key = getattr(cred, "public_key", "") or ""

    if not access_token:
        return {}

    if not public_key:
        public_key = await get_axis_public_key(api_client_id, authorization_key, str(cred.static_ip or ""))
        if public_key:
            cred.public_key = public_key

    url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/feedbroadcast/multi-touch-line"
    body = {
        "securities": [{"scriptId": str(sid).strip()} for sid in script_ids if str(sid).strip()]
    }

    try:
        status_code, resp = await send_axis_api_request(
            method="POST",
            url=url,
            static_ip=str(cred.static_ip or ""),
            authorization_key=authorization_key,
            api_client_id=api_client_id,
            sub_account_id=sub_account_id,
            access_token=access_token,
            public_key=public_key,
            body=body,
        )

        ltp_map: Dict[str, float] = {}
        data = resp.get("data") if isinstance(resp, dict) else resp
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("securities") or data.get("quotes") or data.get("data") or []
            if not isinstance(items, list):
                items = [items] if items else []

        for item in items:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("scriptId") or item.get("scriptid") or item.get("token") or "").strip()
            price_val = None
            for k in ("ltp", "lastPrice", "closePrice", "lp", "price", "touchLine", "close"):
                val = item.get(k)
                if val is not None:
                    try:
                        f_val = float(val)
                        if f_val > 0:
                            price_val = f_val
                            break
                    except (ValueError, TypeError):
                        continue
            if sid and price_val is not None:
                ltp_map[sid] = price_val

        return ltp_map
    except Exception as e:
        logger.debug(f"Failed to fetch live Axis Direct quotes for {cred.client_id}: {e}")
        return {}
