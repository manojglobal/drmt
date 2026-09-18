import logging
from logger.logger import logger_exit
import uuid
from models.schemas import Credential, Position
from core.engine import core_engine
logger = logging.getLogger(__name__)

async def exit_flattrade(pos: Position, cred: Credential) -> bool:
    """Flattrade Exit (Direct or Proxy). Plan specifies direct but can use proxy for consistency if IP matching is needed."""
    url = "https://api.flattrade.in/PlaceOrder"
    
    tran_type = "S" if pos.quantity > 0 else "B" # Flattrade usually uses S/B
    qty = abs(pos.quantity)
    
    app_inst_id = str(uuid.uuid4())
    
    # Payload format based on typical Flattrade API
    payload = {
        "uid": cred.client_id,
        "actid": cred.client_id,
        "exch": pos.exchange,
        "tsym": pos.symbol,
        "qty": str(qty),
        "prc": "0",
        "prd": pos.product_type,
        "trantype": tran_type,
        "prctyp": "MKT",
        "ret": "DAY",
        # Rule: uuid4() for app_inst_id and remarks
        "remarks": app_inst_id,
        "app_inst_id": app_inst_id
    }
    
    try:
        session = core_engine.http_session
        if not session:
            raise RuntimeError("HTTP session not initialized")
            
        async with session.post(url, json={"jData": payload, "jKey": cred.token}) as resp:
            data = await resp.json()
            if data.get("stat") == "Ok":
                return True
            await logger_exit.error(f"Flattrade exit failed for {pos.symbol}: {data}")
            return False
    except Exception as e:
        await logger_exit.error(f"Flattrade exit error for {pos.symbol}: {e}")
        return False
