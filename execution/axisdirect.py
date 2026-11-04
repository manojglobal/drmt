"""
execution/axisdirect.py — Emergency Square-Off Engine for Axis Direct with Forced MPP Safeguard.
Sends POST to https://invest-api.axisdirect.in/trading/orders/place-order.
Enforces Market Protection Pricing (MPP) on Options to prevent broker rejections and slippage.
Features automatic in-flight token refreshes on session errors (codes 1081, 1082, 1200, 1002, 1003).
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Dict, Any

from models.schemas import Credential, Position
from credentials.axisdirect import (
    AXIS_INVEST_BASE_URL,
    get_axis_public_key,
    send_axis_api_request,
    refresh_user_axis_direct_access_token,
)
from logger.logger import logger_exit, logger_api

logger = logging.getLogger("DRMT.SCM")


async def exit_axisdirect(pos: Position, cred: Credential) -> bool:
    """
    Executes an emergency market/MPP exit order on Axis Direct.
    Forces productType='DELIVERY', orderType='RL', and enforces MPP for Options.
    """
    api_client_id = cred.api_key or ""
    authorization_key = cred.api_secret or ""
    sub_account_id = getattr(cred, "sub_account_id", "") or cred.client_id or ""
    access_token = cred.token or ""
    public_key = getattr(cred, "public_key", "") or ""
    static_ip = str(cred.static_ip or "")

    if not access_token:
        await logger_exit.error(f"Axis Direct exit failed for {pos.symbol}: no access_token found for {cred.client_id}")
        return False

    if not public_key:
        public_key = await get_axis_public_key(api_client_id, authorization_key, static_ip)
        if public_key:
            cred.public_key = public_key

    from execution.payload_master import PayloadMaster
    order_values = PayloadMaster.build_axis_payload(pos, cred)
    txn_type = order_values.get("transactionType", "")
    qty = order_values.get("totalQty", 0)
    order_price = order_values.get("orderPrice", 0.0)
    seg = order_values.get("segment", "")
    exchange_formatted = order_values.get("exchange", "")
    is_option = order_price > 0.0

    url = f"{AXIS_INVEST_BASE_URL.rstrip('/')}/trading/orders/place-order"

    await logger_api.info(
        f"📤 [Axis Direct Exit Order Payload] ({cred.client_id}) {pos.symbol} | Side: {txn_type} | Qty: {qty} | Price: {order_price} (MPP={is_option}) | Seg: {seg} | Exch: {exchange_formatted} | Payload: {json.dumps(order_values)}"
    )
    await logger_exit.info(
        f"🚨 [AXIS ORDER DISPATCH] {pos.symbol} | Side: {txn_type} | Qty: {qty} | Price: {order_price} (MPP={is_option}) | Seg: {seg} | Exch: {exchange_formatted} | Target: {cred.client_id}"
    )

    try:
        status_code, porder_resp = await send_axis_api_request(
            method="POST",
            url=url,
            static_ip=static_ip,
            authorization_key=authorization_key,
            api_client_id=api_client_id,
            sub_account_id=sub_account_id,
            access_token=access_token,
            public_key=public_key,
            body=order_values,
        )

        resp_str = json.dumps(porder_resp, default=str) if isinstance(porder_resp, (dict, list)) else str(porder_resp)
        await logger_api.info(f"📥 [Axis Direct Exit Order Response] ({cred.client_id}) {pos.symbol} -> HTTP {status_code}: {resp_str}")

        data = porder_resp.get("data", {}) if isinstance(porder_resp, dict) else porder_resp
        oms_order_id = data.get("omsOrderId") if isinstance(data, dict) else None

        if oms_order_id:
            await logger_api.info(f"✅ [Axis Direct Exit Confirmed] ({cred.client_id}) {pos.symbol}: omsOrderId={oms_order_id}")
            await logger_exit.info(f"✅ [AXIS EXIT CONFIRMED] {pos.symbol} (omsOrderId: {oms_order_id}) for {cred.client_id}")
            return True

        # In-flight token renewal on session error codes
        err_dict = porder_resp.get("error", {}) if isinstance(porder_resp, dict) else {}
        err_code = str(err_dict.get("code") or "")
        err_msg = str(err_dict.get("message") or porder_resp)

        if status_code in (401, 403) or err_code in ("1081", "1082", "1200", "1002", "1003"):
            await logger_exit.warn(
                f"🔑 [AXIS TOKEN EXPIRED] {cred.client_id} (code={err_code or status_code}) — attempting in-flight renewal..."
            )
            renewed = await refresh_user_axis_direct_access_token(cred)
            if renewed:
                status_code, retry_resp = await send_axis_api_request(
                    method="POST",
                    url=url,
                    static_ip=static_ip,
                    authorization_key=cred.api_secret,
                    api_client_id=cred.api_key,
                    sub_account_id=sub_account_id,
                    access_token=cred.token,
                    public_key=cred.public_key,
                    body=order_values,
                )
                retry_str = json.dumps(retry_resp, default=str) if isinstance(retry_resp, (dict, list)) else str(retry_resp)
                await logger_api.info(f"📥 [Axis Direct Exit Order Retry Response] ({cred.client_id}) {pos.symbol} -> HTTP {status_code}: {retry_str}")
                retry_data = retry_resp.get("data", {}) if isinstance(retry_resp, dict) else retry_resp
                retry_oms = retry_data.get("omsOrderId") if isinstance(retry_data, dict) else None
                if retry_oms:
                    await logger_api.info(f"✅ [Axis Direct Exit Confirmed After Renewal] ({cred.client_id}) {pos.symbol}: omsOrderId={retry_oms}")
                    await logger_exit.info(
                        f"✅ [AXIS EXIT CONFIRMED AFTER RENEWAL] {pos.symbol} (omsOrderId: {retry_oms}) for {cred.client_id}"
                    )
                    return True
                else:
                    err_dict = retry_resp.get("error", {}) if isinstance(retry_resp, dict) else {}
                    err_msg = str(err_dict.get("message") or retry_resp)

        await logger_api.error(f"❌ [Axis Direct Exit Rejected] ({cred.client_id}) {pos.symbol}: {err_msg} | Full Response: {resp_str}")
        await logger_exit.error(f"❌ [AXIS EXIT REJECTED] {pos.symbol} for {cred.client_id}: {err_msg}")
        return False

    except Exception as e:
        await logger_api.error(f"❌ [Axis Direct Exit Exception] ({cred.client_id}) {pos.symbol}: {e}")
        await logger_exit.error(f"❌ [AXIS EXIT EXCEPTION] {pos.symbol} for {cred.client_id}: {e}")
        return False
