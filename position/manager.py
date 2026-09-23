import logging
from typing import List, Dict, Any
from models.schemas import Position
from utils.user_mapping import get_client_username

logger = logging.getLogger(__name__)

class PositionManager:
    @staticmethod
    def normalize_positions(raw_list: List[Dict[Any, Any]], broker: str, client_id: str) -> List[Position]:
        """
        Normalizes raw broker data into Position models.
        Applies rules: ID Generation, Zero-Qty active deletion.
        """
        normalized = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
                
            # Safely extract basic fields depending on broker schema mapping
            symbol = (
                raw.get("tradingSymbol")
                or raw.get("symbol")
                or raw.get("symbolname")
                or raw.get("tradingsymbol")
                or raw.get("formattedInstrumentName")
                or raw.get("tsym")
                or raw.get("TrdSym")
                or "UNKNOWN"
            )
            qty = int(
                raw.get("netQuantity")
                or raw.get("quantity")
                or raw.get("netqty")
                or raw.get("Netqty")
                or 0
            )
            
            # Rule: Zero-Qty -> actively skip/delete from tracking
            if qty == 0:
                continue
                
            exchange = (
                raw.get("exchange")
                or raw.get("exch")
                or raw.get("exc")
                or raw.get("Exchange")
                or ""
            )
            product = (
                raw.get("product")
                or raw.get("product_type")
                or raw.get("producttype")
                or raw.get("prd")
                or raw.get("Pcode")
                or ""
            )
            order_type = raw.get("order_type") or ""
            
            # Use provided transaction_type if available, otherwise infer from quantity
            broker_side = (
                raw.get("transactionType")
                or raw.get("transaction_type")
                or raw.get("transaction")
                or raw.get("side")
                or raw.get("trantype")
                or ""
            )
            if broker_side:
                side_str = str(broker_side).upper()
                if side_str in ("B", "BUY"):
                    tran_type = "BUY"
                elif side_str in ("S", "SELL"):
                    tran_type = "SELL"
                else:
                    tran_type = side_str
            else:
                tran_type = "BUY" if qty > 0 else "SELL" # Fallback rule
            
            # Extract valid non-zero average entry price
            avg_price = 0.0
            avg_candidates = [
                raw.get("avgnetprice"),
                raw.get("netAveragePrice"),
                raw.get("netavgprc"),
                raw.get("netprice"),
                raw.get("avg_price"),
                raw.get("sellavgprice") if tran_type == "SELL" else raw.get("buyavgprice"),
                raw.get("cfsellavgprice") if tran_type == "SELL" else raw.get("cfbuyavgprice"),
                raw.get("totalsellavgprice") if tran_type == "SELL" else raw.get("totalbuyavgprice"),
                raw.get("Price"),
                raw.get("buyavgprice"),
                raw.get("sellavgprice"),
            ]
            for candidate in avg_candidates:
                if candidate is not None:
                    try:
                        val = float(candidate)
                        if val > 0:
                            avg_price = val
                            break
                    except (ValueError, TypeError):
                        continue

            # Extract LTP (Last Traded Price)
            ltp = 0.0
            ltp_candidates = [
                raw.get("ltp"),
                raw.get("lp"),
                raw.get("LTP"),
                raw.get("netprice"),
                raw.get("avgnetprice"),
            ]
            for candidate in ltp_candidates:
                if candidate is not None:
                    try:
                        val = float(candidate)
                        if val > 0:
                            ltp = val
                            break
                    except (ValueError, TypeError):
                        continue
            if ltp == 0.0:
                ltp = avg_price

            pnl = 0.0
            pnl_candidates = [
                raw.get("pnl"),
                raw.get("unrealizedPnl"),
                raw.get("realizedPnl"),
                raw.get("urmtom"),
                raw.get("rpnl"),
                raw.get("MtoM"),
                raw.get("m2m"),
                raw.get("M2M"),
            ]
            for candidate in pnl_candidates:
                if candidate is not None:
                    try:
                        pnl = float(candidate)
                        if pnl != 0.0:
                            break
                    except (ValueError, TypeError):
                        continue

            if pnl == 0.0 and avg_price > 0 and ltp > 0 and qty != 0:
                if tran_type == "BUY":
                    pnl = (ltp - avg_price) * abs(qty)
                else:
                    pnl = (avg_price - ltp) * abs(qty)
                pnl = round(pnl, 2)
            
            # Rule: ID Gen as broker:client_id:symbol
            pos_id = f"{broker}:{client_id}:{symbol}"
            
            token_val = str(
                raw.get("instrumentId")
                or raw.get("token")
                or raw.get("symboltoken")
                or raw.get("sym")
                or raw.get("Token")
                or ""
            )
            
            pos = Position(
                id=pos_id,
                broker=broker,
                client_id=client_id,
                username=get_client_username(client_id),
                symbol=symbol,
                token=token_val,
                exchange=exchange,
                product_type=product,
                order_type=order_type,
                transaction_type=tran_type,
                quantity=qty,
                avg_price=avg_price,
                ltp=ltp,
                pnl=pnl
            )
            normalized.append(pos)
            
        # Rule: get_by_client() sorting logic equivalent -> Sort by symbol for consistent UI
        normalized.sort(key=lambda x: x.symbol)
        return normalized
