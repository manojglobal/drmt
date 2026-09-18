import logging
from typing import List, Dict, Any
from models.schemas import Position

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
            # Safely extract basic fields depending on broker schema mapping
            symbol = raw.get("symbol") or raw.get("symbolname") or raw.get("tradingsymbol") or raw.get("tsym") or raw.get("TrdSym") or "UNKNOWN"
            qty = int(raw.get("quantity") or raw.get("netqty") or raw.get("Netqty") or 0)
            
            # Rule: Zero-Qty -> actively skip/delete from tracking
            if qty == 0:
                continue
                
            exchange = raw.get("exchange") or raw.get("exc") or raw.get("Exchange") or ""
            product = raw.get("product_type") or raw.get("producttype") or raw.get("prd") or raw.get("Pcode") or ""
            order_type = raw.get("order_type") or ""
            
            # Use provided transaction_type if available, otherwise infer from quantity
            broker_side = raw.get("transaction_type") or raw.get("transaction") or raw.get("side") or ""
            if broker_side:
                tran_type = str(broker_side).upper()
            else:
                tran_type = "BUY" if qty > 0 else "SELL" # Fallback rule
            
            avg_price = float(raw.get("avg_price") or raw.get("buyavgprice") or raw.get("netavgprc") or raw.get("Price") or 0.0)
            ltp = float(raw.get("ltp") or raw.get("netprice") or raw.get("avgnetprice") or raw.get("LTP") or avg_price or 0.0)
            pnl = float(raw.get("pnl") or raw.get("urmtom") or raw.get("rpnl") or raw.get("MtoM") or 0.0)
            
            # Rule: ID Gen as broker:client_id:symbol
            pos_id = f"{broker}:{client_id}:{symbol}"
            
            token_val = str(raw.get("token") or raw.get("symboltoken") or raw.get("sym") or raw.get("Token") or "")
            
            pos = Position(
                id=pos_id,
                broker=broker,
                client_id=client_id,
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
