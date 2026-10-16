"""
execution/payload_master.py — Centralized Order Payload Master for DRMT V2.
Unifies order payload generation across all 6 supported brokers:
1. AXIS DIRECT
2. KOTAK NEO
3. MSTOCK
4. ALICE BLUE (Web & SDK)
5. FLATTRADE
6. FYERS
Ensures consistent product codes, transaction side inversion on square-off,
and exchange segment routing across the entire execution engine.
"""

import hashlib
import json
import uuid
from typing import Dict, Any, List, Optional
from models.schemas import Position, Credential

from execution.axis_instruments import get_axis_script_id
from execution.kotak_instruments import resolve_kotak_exchange_segment
from execution.flattrade_instruments import resolve_flattrade_exchange
from execution.alice_instruments import resolve_alice_exchange


class PayloadMaster:
    """Central factory for building broker-compliant exit/square-off order payloads."""

    @staticmethod
    def build_axis_payload(pos: Position, cred: Credential) -> Dict[str, Any]:
        """Builds Axis Direct order payload with Market Protection Pricing (MPP) for Options."""
        txn_type = "SELL" if pos.quantity > 0 else "BUY"
        qty = abs(int(pos.quantity))

        exch_upper = str(pos.exchange or "").upper().strip()
        sym_upper = str(pos.symbol or "").upper().strip()
        is_derivative = exch_upper in ("NFO", "BFO", "NSE_FO", "BSE_FO") or any(
            x in sym_upper for x in ("-CE", "-PE", " CE", " PE", " FUT", "-FUT")
        )
        seg = "FO" if is_derivative else "EQ"

        if "_" in exch_upper:
            exchange_formatted = exch_upper
        elif is_derivative:
            exchange_formatted = "BSE_FO" if ("BSE" in exch_upper or "BFO" in exch_upper) else "NSE_FO"
        else:
            exchange_formatted = "BSE_EQ" if ("BSE" in exch_upper) else "NSE_EQ"

        is_option = any(x in sym_upper for x in ("-CE", "-PE", " CE", " PE")) or exch_upper in ("NFO", "BFO")
        if is_option:
            order_price = float(pos.ltp) if pos.ltp > 0 else float(pos.avg_price)
            order_price = round(order_price, 2)
        else:
            order_price = 0.0

        script_id = str(pos.token or "").strip()
        if not script_id.isdigit():
            script_id = get_axis_script_id(pos.symbol) or script_id or str(pos.symbol)
        order_ref_id = str(uuid.uuid4())[:15]

        return {
            "orderRefId": order_ref_id,
            "segment": seg,
            "exchange": exchange_formatted,
            "scriptId": script_id,
            "transactionType": txn_type,
            "productType": "DELIVERY",
            "orderType": "RL",
            "totalQty": qty,
            "disclosedQty": 0,
            "orderPrice": order_price,
            "triggerPrice": 0.0,
            "validityType": "DAY",
            "validityDays": 1,
            "isAmo": False,
        }

    @staticmethod
    def build_kotak_payload(pos: Position) -> Dict[str, str]:
        """Builds Kotak Neo urlencoded form payload for market exit."""
        kotak_txn = "S" if pos.quantity > 0 else "B"
        qty = abs(pos.quantity)

        prod_upper = (pos.product_type or "").upper()
        if prod_upper in ("CNC", "DELIVERY", "C"):
            product_code = "CNC"
        elif prod_upper in ("NRML", "NORMAL", "CARRYFORWARD", "M"):
            product_code = "NRML"
        else:
            product_code = "MIS"

        ex_seg = resolve_kotak_exchange_segment(pos.exchange, pos.symbol)

        tok_val = str(pos.token or "").strip()
        if not tok_val.isdigit():
            # In-memory fast lookup fallback from Kotak instruments
            try:
                from execution.kotak_instruments import _kotak_symbol_to_token
                sym_clean = str(pos.symbol or "").strip().upper()
                base_sym = sym_clean.replace("-EQ", "")
                lookup = (
                    _kotak_symbol_to_token.get(f"{ex_seg}:{sym_clean}")
                    or _kotak_symbol_to_token.get(sym_clean)
                    or _kotak_symbol_to_token.get(f"{ex_seg}:{base_sym}")
                    or _kotak_symbol_to_token.get(base_sym)
                )
                if lookup and str(lookup).isdigit():
                    tok_val = str(lookup)
            except Exception:
                pass

        order_values = {
            "am": "NO",
            "dq": "0",
            "es": ex_seg,
            "mp": "0",
            "pc": product_code,
            "pr": "0",
            "pt": "MKT",
            "qt": str(qty),
            "rt": "DAY",
            "tp": tok_val or str(pos.token or pos.symbol),
            "ts": str(pos.symbol).strip(),
            "tt": kotak_txn,
            "ig": str(uuid.uuid4())[:15],
            "os": "APP",
        }
        return {"jData": json.dumps(order_values)}

    @staticmethod
    def build_mstock_payload(pos: Position, trading_symbol: str) -> Dict[str, Any]:
        """Builds MStock OpenAPI regular market exit payload."""
        prod_type = (pos.product_type or "").upper()
        if prod_type in ("NFO", "BFO", "NCR", "CARRYFORWARD", "NRML"):
            prod_type = "CARRYFORWARD"
        elif prod_type in ("MIS", "INTRADAY"):
            prod_type = "INTRADAY"
        elif prod_type in ("CNC", "DELIVERY"):
            prod_type = "DELIVERY"

        tran_type = "SELL" if pos.quantity > 0 else "BUY"
        qty = abs(pos.quantity)
        order_tag = str(uuid.uuid4()).replace("-", "")[:15]

        return {
            "variety": "NORMAL",
            "tradingsymbol": trading_symbol or pos.symbol,
            "symboltoken": str(pos.token),
            "exchange": pos.exchange,
            "transactiontype": tran_type,
            "ordertype": "MARKET",
            "quantity": str(qty),
            "producttype": prod_type,
            "price": "0",
            "triggerprice": "0",
            "squareoff": "0",
            "stoploss": "0",
            "trailingStopLoss": "",
            "disclosedquantity": "0",
            "duration": "DAY",
            "ordertag": order_tag,
        }

    @staticmethod
    def build_alice_web_payload(pos: Position, cred: Optional[Credential] = None) -> List[Dict[str, Any]]:
        """Builds Alice Blue ANT Web OMK order payload."""
        trans_type = "S" if pos.quantity > 0 else "B"
        qty = abs(pos.quantity)
        prod_upper = str(pos.product_type or "").upper()
        if prod_upper in ("INTRADAY", "MIS", "I"):
            prod = "MIS"
        elif prod_upper in ("CARRYFORWARD", "NRML", "NORMAL", "M"):
            prod = "NRML"
        else:
            prod = "CNC"

        exch = resolve_alice_exchange(pos.exchange, pos.symbol)
        cid = (cred.client_id if cred and cred.client_id else getattr(pos, "client_id", "DRMT_DEFAULT")) or "DRMT_DEFAULT"
        device_id = hashlib.md5(f"DRMT_ALICE_{cid}".encode()).hexdigest()

        return [{
            "deviceNumber": device_id,
            "exchange": exch,
            "token": str(pos.token),
            "tradingSymbol": pos.symbol,
            "ltp": "",
            "qty": str(qty),
            "price": 0,
            "product": prod,
            "priceType": "MKT",
            "orderType": "Regular",
            "transType": trans_type,
            "ret": "DAY",
            "triggerPrice": 0,
            "disclosedQty": "",
            "mktProtection": "",
            "target": "",
            "stopLoss": "",
            "trailingStopLoss": "",
            "orderTag": "DRMT_EXIT",
        }]

    @staticmethod
    def build_alice_sdk_payload(pos: Position, cred: Optional[Credential] = None) -> List[Dict[str, Any]]:
        """Builds Alice Blue Open-API SDK order payload with marketable limit safeguard."""
        trans_type = "SELL" if pos.quantity > 0 else "BUY"
        qty = abs(pos.quantity)
        prod_upper = str(pos.product_type or "").upper()

        if prod_upper in ("INTRADAY", "MIS", "I"):
            prod = "INTRADAY"
        elif prod_upper in ("DELIVERY", "CNC", "LONGTERM", "C"):
            prod = "LONGTERM"
        elif prod_upper in ("MTF",):
            prod = "MTF"
        elif prod_upper in ("CARRYFORWARD", "NRML", "NORMAL", "M"):
            prod = "LONGTERM" if str(pos.exchange).upper() in ("NSE", "BSE") else "NORMAL"
        else:
            prod = "INTRADAY"

        base_price = pos.ltp if (pos.ltp and pos.ltp > 0) else (pos.avg_price if (pos.avg_price and pos.avg_price > 0) else 0.0)
        if base_price > 0:
            raw_price = max(0.05, base_price * 0.99) if trans_type == "SELL" else (base_price * 1.01)
            limit_price = round(round(raw_price / 0.05) * 0.05, 2)
        else:
            limit_price = 0.05

        exch = resolve_alice_exchange(pos.exchange, pos.symbol)
        cid = (cred.client_id if cred and cred.client_id else getattr(pos, "client_id", "DRMT_DEFAULT")) or "DRMT_DEFAULT"
        device_id = hashlib.md5(f"DRMT_ALICE_{cid}".encode()).hexdigest()

        return [{
            "exchange": exch,
            "instrumentId": str(pos.token),
            "transactionType": trans_type,
            "quantity": qty,
            "product": prod,
            "orderComplexity": "REGULAR",
            "orderType": "LIMIT",
            "validity": "DAY",
            "price": limit_price,
            "disclosedQuantity": 0,
            "orderTag": "DRMT_EXIT",
            "deviceId": device_id,
        }]

    @staticmethod
    def build_flattrade_payload(pos: Position, cred: Credential) -> Dict[str, Any]:
        """Builds Flattrade Shoonya JSON order payload."""
        tran_type = "S" if pos.quantity > 0 else "B"
        qty = abs(pos.quantity)

        prod_type = (pos.product_type or "").upper()
        if prod_type in ("CNC", "DELIVERY", "C"):
            prd = "C"
        elif prod_type in ("NRML", "NORMAL", "CARRYFORWARD", "M"):
            prd = "M"
        elif prod_type in ("MIS", "INTRADAY", "I"):
            prd = "I"
        elif prod_type in ("BO", "B"):
            prd = "B"
        elif prod_type in ("CO", "H"):
            prd = "H"
        else:
            prd = prod_type

        exch = resolve_flattrade_exchange(pos.exchange, pos.symbol)
        app_inst_id = str(uuid.uuid4())

        return {
            "uid": cred.client_id,
            "actid": cred.client_id,
            "exch": exch,
            "tsym": pos.symbol,
            "qty": str(qty),
            "prc": "0",
            "prd": prd,
            "trantype": tran_type,
            "prctyp": "MKT",
            "ret": "DAY",
            "remarks": app_inst_id,
            "app_inst_id": app_inst_id,
            "jKey": cred.token,
            "jkey": cred.token,
        }

    @staticmethod
    def build_fyers_payload(pos: Position) -> Dict[str, Any]:
        """Builds Fyers API v3 market exit order payload."""
        tran_type = -1 if pos.quantity > 0 else 1
        qty = abs(pos.quantity)

        prod = (pos.product_type or "").upper()
        if prod in ("CNC", "DELIVERY", "C"):
            fyers_product = "CNC"
        elif prod in ("NRML", "NORMAL", "CARRYFORWARD", "M"):
            fyers_product = "MARGIN"
        elif prod in ("BO", "B"):
            fyers_product = "BO"
        elif prod in ("CO", "H"):
            fyers_product = "CO"
        else:
            fyers_product = "INTRADAY"

        return {
            "symbol": pos.symbol,
            "qty": qty,
            "type": 2,  # Market order
            "side": tran_type,
            "productType": fyers_product,
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
