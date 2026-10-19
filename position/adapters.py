import asyncio
import logging
import json
import random
from typing import List, Dict, Any
from models.schemas import Credential, Position
from credentials.httpclient import proxy_request
from core.engine import core_engine
from position.manager import PositionManager
from execution.mstock_instruments import ensure_mstock_instruments_cached
from credentials.fyers import get_fyers_auth_header
from credentials.kotakneo import get_kotak_headers, get_kotak_base_url
from credentials.axisdirect import send_axis_api_request, get_axis_public_key, AXIS_INVEST_BASE_URL
from execution.axis_instruments import (
    ensure_axis_instruments_cached,
    get_axis_trading_symbol,
    get_axis_close_price,
    fetch_axis_quotes,
)
from execution.kotak_instruments import (
    ensure_kotak_instruments_cached,
    resolve_kotak_exchange_segment,
    get_kotak_exchange_token,
)
from execution.flattrade_instruments import ensure_flattrade_instruments_cached
from execution.alice_instruments import ensure_alice_instruments_cached
from logger.logger import logger_system, logger_monitor

logger = logging.getLogger("DRMT.SCM")

# Tracks last raw payload to avoid redundant log spam when positions are unchanged
_last_raw_payloads: Dict[str, str] = {}

# In-memory entry price cache for Axis Direct positions to prevent floating LTP overwrite
_axis_entry_prices: Dict[str, float] = {}

async def poll_mstock(cred: Credential) -> List[Position]:
    """MStock Position Polling via DRMT Proxy."""
    url = "https://api.mstock.trade/openapi/typeb/portfolio/positions"
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "X-PrivateKey": cred.api_key,
        "X-Mirae-Version": "1",
        "User-Agent": "Mozilla/5.0"
    }
    
    try:
        resp = await proxy_request("GET", url, str(cred.static_ip), headers=headers)
        if isinstance(resp, dict):
            if "data" in resp:
                data = resp.get("data") or []
            elif "json" in resp and isinstance(resp["json"], dict):
                data = resp["json"].get("data") or []
            else:
                data = []
        elif isinstance(resp, list):
            data = resp
        else:
            data = []

        if data:
            payload_str = json.dumps(data, default=str)
            if _last_raw_payloads.get(f"mstock:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"mstock:{cred.client_id}"] = payload_str
                await logger_monitor.info(f"📥 [mStock Positions Payload] ({cred.client_id}): {payload_str}")
        else:
            _last_raw_payloads.pop(f"mstock:{cred.client_id}", None)

        positions = PositionManager.normalize_positions(data, cred.broker, cred.client_id)
        
        # Ensure MStock Script Master is cached in Redis (checks Redis first, non-blocking)
        asyncio.create_task(ensure_mstock_instruments_cached(cred))
        
        # Batch fetch live market LTP from mStock quote API
        exchange_tokens = {}
        for p in positions:
            if p.token and p.exchange:
                exch = str(p.exchange).upper().strip()
                # Defensive check: ensure BSE derivatives use BFO, and NSE derivatives use NFO for quote lookup
                if exch == "BSE" and any(x in p.symbol.upper() for x in ("-CE", "-PE", " CE", " PE", "SENSEX", "BANKEX")):
                    exch = "BFO"
                elif exch == "NSE" and any(x in p.symbol.upper() for x in ("-CE", "-PE", " CE", " PE", "NIFTY", "BANKNIFTY", "FINNIFTY")):
                    exch = "NFO"
                exchange_tokens.setdefault(exch, []).append(str(p.token))

        if exchange_tokens:
            try:
                quote_resp = await asyncio.wait_for(
                    proxy_request(
                        "GET",
                        "https://api.mstock.trade/openapi/typeb/instruments/quote",
                        str(cred.static_ip),
                        headers={
                            "Authorization": f"Bearer {cred.token}",
                            "X-PrivateKey": cred.api_key,
                            "X-Mirae-Version": "1",
                            "Content-Type": "application/json"
                        },
                        json_data={"mode": "LTP", "exchangeTokens": exchange_tokens}
                    ),
                    timeout=2.0
                )
                data_obj = quote_resp.get("data") if isinstance(quote_resp, dict) else None
                fetched = (data_obj.get("fetched") or []) if isinstance(data_obj, dict) else []
                ltp_map = {str(item.get("symbolToken")): float(item.get("ltp", 0)) for item in fetched}

                for p in positions:
                    if p.token in ltp_map and ltp_map[p.token] > 0:
                        p.ltp = ltp_map[p.token]
                        if p.transaction_type == "BUY":
                            p.pnl = round((p.ltp - p.avg_price) * abs(p.quantity), 2)
                        else:
                            p.pnl = round((p.avg_price - p.ltp) * abs(p.quantity), 2)
            except Exception as e:
                logger.warning(f"Could not fetch live LTP quotes for {cred.client_id}: {e}")

        return positions
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            raise PermissionError(f"MStock Auth Error: {e}")
        raise

async def poll_flattrade(cred: Credential) -> List[Position]:
    """Flattrade Position Polling (Direct or via DRMT Proxy)."""
    url = "https://emergeapi.flattrade.in/getPositonBook"
    payload = {"uid": cred.client_id, "actid": cred.client_id, "jKey": cred.token, "jkey": cred.token}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "source": "WEB",
        "device": "Chrome",
        "Origin": "https://emerge.flattrade.in",
        "Referer": "https://emerge.flattrade.in/"
    }
    
    try:
        if cred.static_ip:
            resp = await proxy_request("POST", url, str(cred.static_ip), headers=headers, json_data=payload)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.post(url, json=payload, headers=headers) as r:
                if r.status in (401, 403):
                    raise PermissionError(f"Flattrade Auth Error: HTTP {r.status}")
                resp = await r.json(content_type=None)
        
        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}
        
        # EmergeAPI schema: {"status": "S"|"I"|"E", "errMsg": "...", "positionBookArr": [...]}
        if isinstance(resp, dict):
            status = str(resp.get("status", "")).upper()
            stat = str(resp.get("stat", "")).lower()
            emsg = str(resp.get("errMsg", "") or resp.get("emsg", "")).lower()
            if (
                status in ("I", "E")
                or stat == "not_ok"
                or any(x in emsg for x in ("invalid session", "session expired", "invalid key", "not logged in", "user not logged in"))
            ):
                raise PermissionError(
                    f"Flattrade Auth Error: Invalid/Expired session (status={status}, msg={resp.get('errMsg') or resp.get('emsg')})"
                )
            
            if status != "S" and "positionBookArr" not in resp:
                raise PermissionError(
                    f"Flattrade Auth Error: Unexpected response (status={status}, msg={resp.get('errMsg') or resp.get('emsg')})"
                )
            
            raw_list = resp.get("positionBookArr") or []
            if not isinstance(raw_list, list):
                raw_list = [raw_list] if raw_list else []
        elif isinstance(resp, list):
            raw_list = resp
        else:
            raw_list = []

        if raw_list:
            payload_str = json.dumps(raw_list, default=str)
            if _last_raw_payloads.get(f"flattrade:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"flattrade:{cred.client_id}"] = payload_str
                await logger_monitor.info(f"📥 [Flattrade Positions Payload] ({cred.client_id}): {payload_str}")
        else:
            _last_raw_payloads.pop(f"flattrade:{cred.client_id}", None)
            
        asyncio.create_task(ensure_flattrade_instruments_cached(cred))
        return PositionManager.normalize_positions(raw_list, cred.broker, cred.client_id)
    except Exception as e:
        if "401" in str(e) or "403" in str(e) or isinstance(e, PermissionError):
            raise PermissionError(f"Flattrade Auth Error: {e}")
        raise

async def poll_aliceblue(cred: Credential) -> List[Position]:
    """AliceBlue Position Polling (Direct HTTP)."""
    session = core_engine.http_session
    if not session:
        raise RuntimeError("HTTP Session not initialized")
        
    url = "https://ant.aliceblueonline.com/open-api/od/v1/positions"
    headers = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json"
    }
    
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status in (401, 403):
                raise PermissionError(f"AliceBlue Auth Error: HTTP {resp.status}")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            
            # Check for error or session expiration in 200 OK responses (both stat and status keys)
            stat = str(data.get("stat") or data.get("status") or "").lower()
            emsg = str(data.get("emsg", "") or data.get("message", ""))
            
            if stat == "not_ok":
                # Rule: AliceBlue EC920 means no positions, not an error.
                if "ec920" in emsg.lower():
                    _last_raw_payloads.pop(f"aliceblue:{cred.client_id}", None)
                    return []
                if any(x in emsg.lower() for x in ("login", "session", "token", "unauthorized", "expired")):
                    raise PermissionError(f"AliceBlue Auth Error: {emsg}")
                raise RuntimeError(f"AliceBlue Poll Error: {emsg}")
                
            result = data.get("result", [])
            if isinstance(result, list) and result:
                payload_str = json.dumps(result, default=str)
                if _last_raw_payloads.get(f"aliceblue:{cred.client_id}") != payload_str:
                    _last_raw_payloads[f"aliceblue:{cred.client_id}"] = payload_str
                    await logger_monitor.info(f"📥 [AliceBlue Positions Payload] ({cred.client_id}): {payload_str}")
            else:
                _last_raw_payloads.pop(f"aliceblue:{cred.client_id}", None)

            asyncio.create_task(ensure_alice_instruments_cached(cred))
            if isinstance(result, list):
                return PositionManager.normalize_positions(result, cred.broker, cred.client_id)
            return []
    except PermissionError:
        raise
    except Exception as e:
        if "401" in str(e) or "403" in str(e):
            raise PermissionError(f"AliceBlue Auth Error: {e}")
        raise RuntimeError(f"AliceBlue Poll Error: {e}")


async def poll_fyers(cred: Credential) -> List[Position]:
    """Fyers Position Polling (Direct or via DRMT Proxy)."""
    url = "https://api-t1.fyers.in/api/v3/positions"
    auth_header = get_fyers_auth_header(cred)
    if not auth_header:
        raise PermissionError(f"Fyers Auth Error: no token available for {cred.client_id}")

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (DRMT Fyers Adapter)"
    }

    try:
        if cred.static_ip:
            resp = await proxy_request("GET", url, str(cred.static_ip), headers=headers)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.get(url, headers=headers) as r:
                if r.status in (401, 403):
                    raise PermissionError(f"Fyers Auth Error: HTTP {r.status}")
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}

        if isinstance(resp, dict):
            code_val = resp.get("code")
            s_val = str(resp.get("s", "")).lower()
            msg = str(resp.get("message") or resp.get("errMsg") or "").lower()

            if code_val in (401, 403) or any(x in msg for x in ("token expired", "invalid token", "unauthorized", "session expired")):
                raise PermissionError(f"Fyers Auth Error for {cred.client_id}: {msg} (code={code_val})")

            raw_list = resp.get("netPositions") or resp.get("positions") or []
            if not isinstance(raw_list, list):
                raw_list = []
        elif isinstance(resp, list):
            raw_list = resp
        else:
            raw_list = []

        if raw_list:
            payload_str = json.dumps(raw_list, default=str)
            if _last_raw_payloads.get(f"fyers:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"fyers:{cred.client_id}"] = payload_str
                await logger_monitor.info(f"📥 [Fyers Positions Payload] ({cred.client_id}): {payload_str}")
        else:
            _last_raw_payloads.pop(f"fyers:{cred.client_id}", None)

        return PositionManager.normalize_positions(raw_list, cred.broker, cred.client_id)
    except Exception as e:
        if "401" in str(e) or "403" in str(e) or isinstance(e, PermissionError):
            raise PermissionError(f"Fyers Auth Error: {e}")
        raise


async def poll_kotakneo(cred: Credential) -> List[Position]:
    """Kotak Neo Position Polling (GET /quick/user/positions via Proxy or Direct)."""
    base_url = get_kotak_base_url(cred)
    headers = get_kotak_headers(cred)

    if not headers.get("Auth"):
        raise PermissionError(f"Kotak Neo Auth Error: missing access_token for {cred.client_id}")

    url = f"{base_url}/quick/user/positions"
    try:
        if cred.static_ip:
            resp = await proxy_request("GET", url, str(cred.static_ip), headers=headers)
        else:
            session = core_engine.http_session
            if not session:
                raise RuntimeError("HTTP session not initialized")
            async with session.get(url, headers=headers) as r:
                if r.status in (401, 403):
                    raise PermissionError(f"Kotak Neo Auth Error: HTTP {r.status} Unauthorized")
                resp = await r.json(content_type=None)

        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except Exception:
                resp = {}

        if isinstance(resp, dict):
            stat = str(resp.get("stat") or "").strip().lower()
            st_code = resp.get("stCode")
            err_msg = str(resp.get("errMsg") or resp.get("message") or "")
            is_no_data = "no data" in err_msg.lower() or "no data" in stat
            if not is_no_data and (st_code in (100010, 401, 100008, 403, 1037) or stat == "not_ok"):
                raise PermissionError(
                    f"Kotak Neo Auth Error: Invalid/Expired session (stCode={st_code}, msg={err_msg or 'Session expired'})"
                )
            raw_list = [] if is_no_data else (resp.get("data") or [])
            if not isinstance(raw_list, list):
                raw_list = [raw_list] if raw_list else []
        elif isinstance(resp, list):
            raw_list = resp
        else:
            raw_list = []

        if raw_list:
            payload_str = json.dumps(raw_list, default=str)
            if _last_raw_payloads.get(f"kotak:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"kotak:{cred.client_id}"] = payload_str
                await logger_monitor.info(f"📥 [Kotak Neo Positions Payload] ({cred.client_id}): {payload_str}")
        else:
            _last_raw_payloads.pop(f"kotak:{cred.client_id}", None)

        positions = PositionManager.normalize_positions(raw_list, cred.broker, cred.client_id)
        if not positions:
            return positions

        # Ensure Kotak Neo Script Master is cached in Redis (non-blocking)
        asyncio.create_task(ensure_kotak_instruments_cached(cred))

        # Batch fetch real-time market LTP via Kotak Neo quotes endpoint
        neo_tokens = []
        for p in positions:
            if not p.token or not str(p.token).isdigit():
                tok_lookup = await get_kotak_exchange_token(p.symbol, p.exchange)
                if tok_lookup:
                    p.token = tok_lookup
            if p.token:
                seg = resolve_kotak_exchange_segment(p.exchange, p.symbol)
                neo_tokens.append(f"{seg}|{p.token}")

        if neo_tokens:
            try:
                sym_str = ",".join(neo_tokens[:50])
                quote_url = f"{base_url}/script-details/1.0/quotes/neosymbol/{sym_str}/ltp"
                if cred.static_ip:
                    quote_resp = await asyncio.wait_for(
                        proxy_request("GET", quote_url, str(cred.static_ip), headers=headers),
                        timeout=2.0
                    )
                else:
                    session = core_engine.http_session
                    if session:
                        async with session.get(quote_url, headers=headers) as qr:
                            quote_resp = await qr.json(content_type=None)
                    else:
                        quote_resp = None

                items = quote_resp if isinstance(quote_resp, list) else (quote_resp.get("data") if isinstance(quote_resp, dict) else [])
                if isinstance(items, list):
                    ltp_map = {}
                    for item in items:
                        if isinstance(item, dict):
                            t_id = str(item.get("exchange_token") or item.get("instrument_token") or "").strip()
                            lp = item.get("ltp")
                            if t_id and lp is not None:
                                try:
                                    ltp_map[t_id] = float(lp)
                                except (ValueError, TypeError):
                                    pass
                    for p in positions:
                        if p.token in ltp_map and ltp_map[p.token] > 0:
                            p.ltp = round(ltp_map[p.token], 2)
                        elif p.ltp == 0.0 and p.avg_price > 0:
                            p.ltp = p.avg_price

                        if p.avg_price > 0 and p.ltp > 0 and p.quantity != 0:
                            if p.transaction_type == "BUY":
                                p.pnl = round((p.ltp - p.avg_price) * abs(p.quantity), 2)
                            else:
                                p.pnl = round((p.avg_price - p.ltp) * abs(p.quantity), 2)
            except Exception as e:
                logger.debug(f"Could not fetch Kotak Neo quotes for {cred.client_id}: {e}")
                for p in positions:
                    if p.ltp == 0.0 and p.avg_price > 0:
                        p.ltp = p.avg_price
                    if p.avg_price > 0 and p.ltp > 0 and p.quantity != 0:
                        if p.transaction_type == "BUY":
                            p.pnl = round((p.ltp - p.avg_price) * abs(p.quantity), 2)
                        else:
                            p.pnl = round((p.avg_price - p.ltp) * abs(p.quantity), 2)

        return positions
    except Exception as e:
        if "401" in str(e) or "403" in str(e) or "100010" in str(e) or isinstance(e, PermissionError):
            raise PermissionError(f"Kotak Neo Auth Error: {e}")
        raise


async def poll_axisdirect(cred: Credential) -> List[Position]:
    """Axis Direct Position Polling via RAPID API (get-positions with order-book fallback)."""
    api_client_id = cred.api_key or ""
    authorization_key = cred.api_secret or ""
    sub_account_id = getattr(cred, "sub_account_id", "") or cred.client_id or ""
    access_token = cred.token or ""
    public_key = getattr(cred, "public_key", "") or ""

    if not access_token:
        raise PermissionError(f"Axis Direct Auth Error: missing access_token for {cred.client_id}")

    if not public_key:
        public_key = await get_axis_public_key(api_client_id, authorization_key, str(cred.static_ip or ""))
        if public_key:
            cred.public_key = public_key

    all_raw_items = []
    try:
        # 1. First probe portfolio positions endpoint
        for seg in ("FO", "EQ"):
            url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/trading/portfolio/get-positions"
            status_code, resp = await send_axis_api_request(
                method="POST",
                url=url,
                static_ip=str(cred.static_ip or ""),
                authorization_key=authorization_key,
                api_client_id=api_client_id,
                sub_account_id=sub_account_id,
                access_token=access_token,
                public_key=public_key,
                body={"segment": seg, "interop": True, "type": "open"},
            )

            err_dict = resp.get("error", {}) if isinstance(resp, dict) else {}
            err_code = str(err_dict.get("code") or "")
            if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
                raise PermissionError(
                    f"Axis Direct Auth Error for {cred.client_id} (code={err_code or status_code}): {err_dict.get('message', 'Session expired')}"
                )

            data = resp.get("data") if isinstance(resp, dict) else resp
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("positions") or data.get("positionList") or data.get("data") or []
                if not isinstance(items, list):
                    items = [items] if items else []

            if isinstance(items, list):
                all_raw_items.extend(items)

        # 2. If portfolio positions were empty or returned 404, fallback to order-book to derive positions
        if not all_raw_items:
            for seg in ("FO", "EQ"):
                ob_url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/trading/orders/get-order-book"
                status_code, ob_resp = await send_axis_api_request(
                    method="POST",
                    url=ob_url,
                    static_ip=str(cred.static_ip or ""),
                    authorization_key=authorization_key,
                    api_client_id=api_client_id,
                    sub_account_id=sub_account_id,
                    access_token=access_token,
                    public_key=public_key,
                    body={"segment": seg, "omsOrderId": "", "orderStatus": "-1"},
                )
                err_dict = ob_resp.get("error", {}) if isinstance(ob_resp, dict) else {}
                err_code = str(err_dict.get("code") or "")
                if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
                    raise PermissionError(
                        f"Axis Direct Auth Error for {cred.client_id} (code={err_code or status_code}): {err_dict.get('message', 'Session expired')}"
                    )

                ob_data = ob_resp.get("data") if isinstance(ob_resp, dict) else ob_resp
                ob_orders = []
                if isinstance(ob_data, dict):
                    ob_orders = ob_data.get("orders") or []
                elif isinstance(ob_data, list):
                    ob_orders = ob_data
                if isinstance(ob_orders, list):
                    all_raw_items.extend(ob_orders)

        # If orderbook orders, derive open net positions from executed trades
        positions_to_normalize = []
        if all_raw_items and any("orderStatus" in x or "omsOrderId" in x for x in all_raw_items if isinstance(x, dict)):
            # Deduplicate raw order records by unique order identifier (omsOrderId / orderRefId)
            seen_order_ids = set()
            unique_orders = []
            for o in all_raw_items:
                if not isinstance(o, dict):
                    continue
                oid = str(o.get("omsOrderId") or o.get("orderRefId") or o.get("exchangeOrderId") or "").strip()
                if oid:
                    if oid in seen_order_ids:
                        continue
                    seen_order_ids.add(oid)
                unique_orders.append(o)

            order_groups: Dict[str, Dict[str, Any]] = {}
            for o in unique_orders:
                if not isinstance(o, dict):
                    continue
                st = str(o.get("orderStatus") or "").upper()
                if st not in ("TRADED", "COMPLETE", "EXECUTED"):
                    continue
                sym = str(o.get("scriptId") or o.get("tradingSymbol") or "").strip()
                if not sym:
                    continue
                exch = str(o.get("exchange") or "").strip()
                key = f"{exch}:{sym}"
                txn = str(o.get("transactionType") or "").upper()
                qty = float(o.get("executedQty") or o.get("tradeQty") or o.get("totalQty") or 0)
                
                # Extract executed price across multiple potential broker keys (both camelCase and snake_case)
                price = 0.0
                for pk in (
                    "average_price", "avg_price", "averagePrice", "avgPrice",
                    "tradedPrice", "tradePrice", "traded_price", "trade_price",
                    "executionPrice", "execution_price", "executedPrice", "executed_price",
                    "orderPrice", "order_price", "price", "rate", "costPrice", "buyAvgPrice"
                ):
                    pv = o.get(pk)
                    if pv is not None:
                        try:
                            fp = float(pv)
                            if fp > 0:
                                price = fp
                                break
                        except (ValueError, TypeError):
                            pass

                # Also inspect nested trade details if present in order record
                if price == 0.0:
                    for nested_k in ("tradeDetails", "executedDetails", "trades", "orderDetails", "fillDetails"):
                        nested_items = o.get(nested_k)
                        if isinstance(nested_items, list) and nested_items:
                            for n_item in nested_items:
                                if isinstance(n_item, dict):
                                    for npk in ("tradePrice", "tradedPrice", "price", "rate", "avgPrice", "averagePrice"):
                                        nv = n_item.get(npk)
                                        if nv is not None:
                                            try:
                                                fp = float(nv)
                                                if fp > 0:
                                                    price = fp
                                                    break
                                            except (ValueError, TypeError):
                                                pass
                                if price > 0:
                                    break
                        if price > 0:
                            break

                prod = str(o.get("productType") or "DELIVERY").strip()

                if key not in order_groups:
                    order_groups[key] = {
                        "scriptId": sym,
                        "exchange": exch,
                        "product": prod,
                        "buyQty": 0.0,
                        "sellQty": 0.0,
                        "buyVal": 0.0,
                        "sellVal": 0.0,
                    }
                if txn in ("BUY", "B"):
                    order_groups[key]["buyQty"] += qty
                    order_groups[key]["buyVal"] += qty * price
                else:
                    order_groups[key]["sellQty"] += qty
                    order_groups[key]["sellVal"] += qty * price

            for key, g in order_groups.items():
                net_qty = g["buyQty"] - g["sellQty"]
                if net_qty != 0:
                    if net_qty > 0 and g["buyQty"] > 0:
                        avg_p = g["buyVal"] / g["buyQty"]
                    elif net_qty < 0 and g["sellQty"] > 0:
                        avg_p = g["sellVal"] / g["sellQty"]
                    else:
                        avg_p = 0.0
                    positions_to_normalize.append({
                        "scriptId": g["scriptId"],
                        "exchange": g["exchange"],
                        "product": g["product"],
                        "netQty": int(net_qty),
                        "avgPrice": round(avg_p, 2),
                        "transactionType": "BUY" if net_qty > 0 else "SELL",
                    })
        else:
            # Deduplicate portfolio positions if identical items returned across segments
            seen_pos_keys = set()
            for it in all_raw_items:
                if isinstance(it, dict):
                    pkey = (str(it.get("exchange") or ""), str(it.get("scriptId") or it.get("token") or ""), str(it.get("product") or ""))
                    if pkey != ("", "", ""):
                        if pkey in seen_pos_keys:
                            continue
                        seen_pos_keys.add(pkey)
                positions_to_normalize.append(it)

        # Ensure Axis Direct Script Master is cached in Redis (non-blocking)
        asyncio.create_task(ensure_axis_instruments_cached(cred))

        # Resolve official human-readable trading symbols (e.g. 223787 -> WIPRO)
        for p in positions_to_normalize:
            sid = str(p.get("scriptId") or p.get("token") or "").strip()
            if sid:
                real_sym = await get_axis_trading_symbol(sid, str(p.get("exchange") or ""))
                if real_sym:
                    p["tradingSymbol"] = real_sym
                    p["symbol"] = real_sym

        if positions_to_normalize:
            payload_str = json.dumps(positions_to_normalize, default=str)
            if _last_raw_payloads.get(f"axis:{cred.client_id}") != payload_str:
                _last_raw_payloads[f"axis:{cred.client_id}"] = payload_str
                await logger_monitor.info(f"📥 [Axis Direct Positions Payload] ({cred.client_id}): {payload_str}")
        else:
            _last_raw_payloads.pop(f"axis:{cred.client_id}", None)

        positions = PositionManager.normalize_positions(positions_to_normalize, cred.broker, cred.client_id)

        # Batch fetch real-time market LTP via Axis Direct feed quote API
        token_ids = [str(p.token) for p in positions if p.token]
        if token_ids:
            try:
                ltp_map = await asyncio.wait_for(
                    fetch_axis_quotes(token_ids, cred),
                    timeout=2.0
                )
                for p in positions:
                    ltp_val = ltp_map.get(str(p.token))
                    if not ltp_val or ltp_val <= 0:
                        # Fallback to previous close price from security master
                        ltp_val = get_axis_close_price(str(p.token))

                    if ltp_val and ltp_val > 0:
                        p.ltp = round(float(ltp_val), 2)

                    # Manage entry price persistence to prevent it from floating with live LTP
                    cache_key = f"{cred.client_id}:{p.token}"
                    if p.avg_price > 0:
                        _axis_entry_prices[cache_key] = p.avg_price
                    elif cache_key in _axis_entry_prices and _axis_entry_prices[cache_key] > 0:
                        p.avg_price = _axis_entry_prices[cache_key]
                        p.cash = round(abs(p.quantity) * p.avg_price, 2)
                        p.cash_value = p.cash
                    else:
                        # Initial entry price fallback: prefer previous close price, then first live LTP
                        cp = get_axis_close_price(str(p.token))
                        init_price = cp if cp > 0 else p.ltp
                        if init_price > 0:
                            p.avg_price = round(float(init_price), 2)
                            _axis_entry_prices[cache_key] = p.avg_price
                            p.cash = round(abs(p.quantity) * p.avg_price, 2)
                            p.cash_value = p.cash

                    if p.ltp > 0 and p.avg_price > 0 and p.quantity != 0:
                        if p.transaction_type == "BUY":
                            p.pnl = round((p.ltp - p.avg_price) * abs(p.quantity), 2)
                        else:
                            p.pnl = round((p.avg_price - p.ltp) * abs(p.quantity), 2)
            except Exception as e:
                logger.debug(f"Could not fetch Axis quotes: {e}")
                for p in positions:
                    cache_key = f"{cred.client_id}:{p.token}"
                    if p.ltp == 0.0:
                        cp = get_axis_close_price(str(p.token))
                        if cp > 0:
                            p.ltp = cp

                    if p.avg_price > 0:
                        _axis_entry_prices[cache_key] = p.avg_price
                    elif cache_key in _axis_entry_prices and _axis_entry_prices[cache_key] > 0:
                        p.avg_price = _axis_entry_prices[cache_key]
                        p.cash = round(abs(p.quantity) * p.avg_price, 2)
                        p.cash_value = p.cash
                    elif p.ltp > 0:
                        p.avg_price = p.ltp
                        _axis_entry_prices[cache_key] = p.avg_price
                        p.cash = round(abs(p.quantity) * p.avg_price, 2)
                        p.cash_value = p.cash

                    if p.ltp > 0 and p.avg_price > 0 and p.quantity != 0:
                        if p.transaction_type == "BUY":
                            p.pnl = round((p.ltp - p.avg_price) * abs(p.quantity), 2)
                        else:
                            p.pnl = round((p.avg_price - p.ltp) * abs(p.quantity), 2)

        return positions
    except Exception as e:
        if "401" in str(e) or "403" in str(e) or isinstance(e, PermissionError):
            raise PermissionError(f"Axis Direct Auth Error: {e}")
        raise


async def broker_poll(cred: Credential) -> List[Position]:
    """Router for broker polling based on credential."""
    if cred.broker.upper() == "FAKE":
        return []
    elif cred.broker.upper() == "MSTOCK":
        return await poll_mstock(cred)
    elif cred.broker.upper() == "FLATTRADE":
        return await poll_flattrade(cred)
    elif cred.broker.upper() in ("ALICE BLUE", "ALICEBLUE"):
        return await poll_aliceblue(cred)
    elif cred.broker.upper() == "FYERS":
        return await poll_fyers(cred)
    elif cred.broker.upper() in ("KOTAK", "KOTAK NEO", "KOTAKNEO"):
        return await poll_kotakneo(cred)
    elif cred.broker.upper() in ("AXIS", "AXIS DIRECT", "AXISDIRECT"):
        return await poll_axisdirect(cred)
    else:
        logger.warning(f"Unknown broker: {cred.broker}")
        return []
