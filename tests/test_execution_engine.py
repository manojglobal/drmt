"""Unit tests for execution engine, all 6 broker exit adapters, debounce TTL, and instrument resolvers."""

import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from execution.alice_instruments import resolve_alice_exchange
from execution.aliceblue import _check_order_success, exit_aliceblue
from execution.axisdirect import exit_axisdirect
from execution.engine import (
    _recently_exited_positions,
    get_recently_exited_ids,
    is_recently_exited,
    mark_position_as_exited,
    mass_exit,
)
from execution.flattrade import exit_flattrade
from execution.flattrade_instruments import resolve_flattrade_exchange
from execution.fyers import exit_fyers
from execution.kotak_instruments import resolve_kotak_exchange_segment
from execution.kotakneo import exit_kotakneo
from execution.mstock import exit_mstock
from execution.payload_master import PayloadMaster
from models.schemas import Credential, Position
from tests.helpers import suppress_email_notifications


def _pos(broker: str, cid: str, symbol: str, qty: int, exchange: str = "NSE", prd: str = "INTRADAY", ltp: float = 100.0) -> Position:
    return Position(
        id=f"{broker}:{cid}:{symbol}:{exchange}:{prd}",
        broker=broker,
        client_id=cid,
        symbol=symbol,
        token="2885",
        exchange=exchange,
        product_type=prd,
        order_type="MKT",
        transaction_type="BUY" if qty > 0 else "SELL",
        quantity=qty,
        avg_price=100.0,
        ltp=ltp,
        pnl=0.0,
    )


class TestExecutionEngineAndAdapters(unittest.IsolatedAsyncioTestCase):
    """Tests mass_exit orchestration, 20s anti-echo cache, marketable limit pricing, and broker exits."""

    def setUp(self):
        _recently_exited_positions.clear()

    def tearDown(self):
        _recently_exited_positions.clear()

    def test_anti_echo_debounce_cache_and_ttl_expiry(self):
        """mark_position_as_exited matches exact broker/client/symbol/exchange/product and expires cleanly after TTL."""
        exited_pos = _pos("FLATTRADE", "FZ59716", "WIPRO-EQ", 1, exchange="NSE", prd="INTRADAY")
        mark_position_as_exited(exited_pos)

        self.assertTrue(is_recently_exited(exited_pos))
        self.assertTrue(is_recently_exited("FLATTRADE:FZ59716:WIPRO-EQ:NSE:INTRADAY"))

        # Must not suppress different broker, -EQ stripped symbol, different product, or different exchange
        self.assertFalse(is_recently_exited(_pos("MSTOCK", "FZ59716", "WIPRO", 1, exchange="NSE", prd="INTRADAY")))
        self.assertFalse(is_recently_exited(_pos("FLATTRADE", "FZ59716", "WIPRO", 1, exchange="NSE", prd="INTRADAY")))
        self.assertFalse(is_recently_exited(_pos("FLATTRADE", "FZ59716", "WIPRO-EQ", 1, exchange="NSE", prd="CNC")))
        self.assertFalse(is_recently_exited(_pos("FLATTRADE", "FZ59716", "WIPRO-EQ", 1, exchange="BSE", prd="INTRADAY")))

        # Must not collide when Fyers symbols contain ':' (e.g. NSE:SBIN-EQ vs NSE:INFY-EQ)
        fyers_exited = _pos("FYERS", "XR10079", "NSE:SBIN-EQ", 1, exchange="NSE", prd="INTRADAY")
        fyers_other = _pos("FYERS", "XR10079", "NSE:INFY-EQ", 1, exchange="NSE", prd="INTRADAY")
        mark_position_as_exited(fyers_exited)
        self.assertTrue(is_recently_exited(fyers_exited))
        self.assertFalse(is_recently_exited(fyers_other))

        # Simulate elapsed time past 20s TTL
        for k in list(_recently_exited_positions.keys()):
            _recently_exited_positions[k] = time.monotonic() - 25.0
        self.assertFalse(is_recently_exited(exited_pos, ttl_seconds=20.0))
        self.assertEqual(get_recently_exited_ids(ttl_seconds=20.0), set())

    def test_flattrade_sdk_marketable_limit_payload_and_instrument_resolvers(self):
        """Flattrade SDK builds LMT payload with 5% option buffer / 1% equity buffer rounded to 0.05."""
        cred = Credential(
            broker="FLATTRADE", client_id="FZ59716", token="sdk_tok",
            api_key="ak", api_secret="as", fingerprint="FLATTRADE:FZ59716", status="working"
        )
        # Long option exit (SELL) with LTP=100 -> 100 * 0.95 = 95.0
        opt_pos = _pos("FLATTRADE", "FZ59716", "NIFTY26NOV25000CE", 50, exchange="NFO", prd="NRML", ltp=100.0)
        form_opt = PayloadMaster.build_flattrade_sdk_payload(opt_pos, cred)
        self.assertIn("&jKey=sdk_tok", form_opt)
        j_opt = json.loads(form_opt.split("jData=")[1].split("&jKey=")[0])
        self.assertEqual(j_opt["prctyp"], "LMT")
        self.assertEqual(j_opt["trantype"], "S")
        self.assertEqual(j_opt["prd"], "M")
        self.assertEqual(float(j_opt["prc"]), 95.0)

        # Short equity exit (BUY) with LTP=200 -> 200 * 1.01 = 202.0
        eq_pos = _pos("FLATTRADE", "FZ59716", "RELIANCE-EQ", -5, exchange="NSE", prd="MIS", ltp=200.0)
        form_eq = PayloadMaster.build_flattrade_sdk_payload(eq_pos, cred)
        j_eq = json.loads(form_eq.split("jData=")[1].split("&jKey=")[0])
        self.assertEqual(j_eq["trantype"], "B")
        self.assertEqual(j_eq["prd"], "I")
        self.assertEqual(float(j_eq["prc"]), 202.0)

        # Exchange resolvers
        self.assertEqual(resolve_flattrade_exchange("BSE", "SENSEX26N1282300PE"), "BFO")
        self.assertEqual(resolve_alice_exchange("NSE", "NIFTY26NOV24000-CE"), "NFO")
        self.assertEqual(resolve_kotak_exchange_segment("BSE", "SENSEX26N1282300-CE"), "bse_fo")
        self.assertEqual(resolve_kotak_exchange_segment("BFO", "SENSEX26N1282300CE"), "bse_fo")
        self.assertEqual(resolve_kotak_exchange_segment("BSE", "RELIANCE"), "bse_cm")

    async def test_mass_exit_partial_failures_and_missing_credentials(self):
        """mass_exit marks only successful exits, reports missing credentials, and tracks totals."""
        p_ok = _pos("FLATTRADE", "FZ59716", "WIPRO-EQ", 1)
        p_fail = _pos("MSTOCK", "P645M2", "RELIANCE-EQ", 2)
        p_missing = _pos("FYERS", "UNKNOWN_CID", "SBIN-EQ", 3)

        creds = [
            Credential(broker="FLATTRADE", client_id="FZ59716", token="t1", fingerprint="FLATTRADE:FZ59716", status="working"),
            Credential(broker="MSTOCK", client_id="P645M2", token="t2", fingerprint="MSTOCK:P645M2", status="working"),
        ]

        async with suppress_email_notifications():
            with (
                patch("execution.engine.exit_flattrade", new=AsyncMock(return_value=True)),
                patch("execution.engine.exit_mstock", new=AsyncMock(return_value=False)),
            ):
                ok_count, results = await mass_exit([p_ok, p_fail, p_missing], creds)

        self.assertEqual(ok_count, 1)
        self.assertEqual(len(results), 3)
        self.assertTrue(is_recently_exited(p_ok.id))
        self.assertFalse(is_recently_exited(p_fail.id))
        missing_res = [r for r in results if r["symbol"] == "SBIN-EQ"][0]
        self.assertFalse(missing_res["success"])
        self.assertIn("No active credential", missing_res["message"])

    async def test_flattrade_sdk_exit_and_reauth_retry(self):
        """Flattrade SDK exit handles Not_Ok expired session with automatic re-auth retry."""
        pos = _pos("FLATTRADE", "FZ59716", "WIPRO-EQ", 1)
        cred = Credential(
            broker="FLATTRADE", client_id="FZ59716", token="old_tok", api_key="ak", api_secret="as",
            static_ip="172.31.8.133", fingerprint="FLATTRADE:FZ59716", status="working"
        )
        async with suppress_email_notifications():
            with (
                patch("execution.flattrade.settings.FLATTRADE_FLOW", "SDK"),
                patch("execution.flattrade.login_flattrade", new=AsyncMock(return_value="fresh_tok")),
                patch("execution.flattrade.proxy_request", new=AsyncMock(side_effect=[
                    {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"},
                    {"stat": "Ok", "norenordno": "2611190001"},
                ])),
            ):
                self.assertTrue(await exit_flattrade(pos, cred))
                self.assertEqual(cred.token, "fresh_tok")

    async def test_mstock_fyers_aliceblue_kotak_axis_exit_adapters(self):
        """Verify all remaining 5 broker exit adapters return True on confirmed orders and False on rejections."""
        async with suppress_email_notifications():
            # 1. mStock
            ms_pos = _pos("MSTOCK", "P645M2", "WIPRO", 1, prd="NRML")
            ms_cred = Credential(broker="MSTOCK", client_id="P645M2", token="mt", api_key="mk", static_ip="1.1.1.1", fingerprint="MSTOCK:P645M2", status="working")
            with (
                patch("execution.mstock.get_mstock_trading_symbol", new=AsyncMock(return_value="WIPRO-EQ")),
                patch("execution.mstock.proxy_request", new=AsyncMock(return_value={"status": True, "data": {"orderid": "MS_1"}})),
            ):
                self.assertTrue(await exit_mstock(ms_pos, ms_cred))

            # 2. Fyers
            fy_pos = _pos("FYERS", "XR10079", "NSE:NIFTY26NOV24000CE", -25, exchange="NFO")
            fy_cred = Credential(broker="FYERS", client_id="XR10079", token="ft", api_key="fa", static_ip="1.1.1.1", fingerprint="FYERS:XR10079", status="working")
            with patch("execution.fyers.proxy_request", new=AsyncMock(return_value={"s": "ok", "id": "FY_1"})):
                self.assertTrue(await exit_fyers(fy_pos, fy_cred))
            with patch("execution.fyers.proxy_request", new=AsyncMock(return_value={"s": "error", "code": -99, "message": "Margin shortfall"})):
                self.assertFalse(await exit_fyers(fy_pos, fy_cred))

            # 3. AliceBlue (Web + SDK validation helpers)
            self.assertEqual(_check_order_success({"result": [{"orderNo": "AB_99"}]}, "WEB"), (True, "AB_99"))
            self.assertFalse(_check_order_success({"result": [{"stat": "Not_Ok", "message": "Rejected"}]}, "SDK")[0])
            ab_pos = _pos("ALICE BLUE", "1923416", "SBIN-EQ", 10)
            ab_cred = Credential(broker="ALICE BLUE", client_id="1923416", token="abt", static_ip="1.1.1.1", fingerprint="ALICE BLUE:1923416", status="working")
            with (
                patch("execution.aliceblue.settings.ALICE_BLUE_FLOW", "WEB"),
                patch("execution.aliceblue.proxy_request", new=AsyncMock(return_value={"result": [{"orderNo": "AB_WEB_1"}]})),
            ):
                self.assertTrue(await exit_aliceblue(ab_pos, ab_cred))

            # 4. Kotak Neo (including in-flight 100010 renewal)
            k_pos = _pos("KOTAK NEO", "VFIU2", "HDFCBANK-EQ", 5)
            k_cred = Credential(broker="KOTAK NEO", client_id="VFIU2", token="kt", api_key="ka", sid="ks", static_ip="1.1.1.1", fingerprint="KOTAK NEO:VFIU2", status="working")
            with (
                patch("execution.kotakneo.login_kotakneo", new=AsyncMock(return_value="kt_new")),
                patch("execution.kotakneo._send_kotak_order", new=AsyncMock(side_effect=[
                    {"stat": "Not_Ok", "stCode": 100010, "errMsg": "Invalid session"},
                    {"stat": "Ok", "nOrdNo": "K_ORD_1"},
                ])),
            ):
                self.assertTrue(await exit_kotakneo(k_pos, k_cred))

            # 5. Axis Direct (including MPP options order + 1081 token renewal)
            ax_pos = _pos("AXIS DIRECT", "11939", "NIFTY-CE", 50, exchange="NFO", ltp=120.0)
            ax_cred = Credential(
                broker="AXIS DIRECT", client_id="11939", token="at", api_key="aa", api_secret="as",
                public_key="pub_k", static_ip="1.1.1.1", fingerprint="AXIS DIRECT:11939", status="working"
            )
            with (
                patch("execution.axisdirect.refresh_user_axis_direct_access_token", new=AsyncMock(return_value=True)),
                patch("execution.axisdirect.send_axis_api_request", new=AsyncMock(side_effect=[
                    (401, {"error": {"code": "1081", "message": "Token expired"}}),
                    (200, {"data": {"omsOrderId": "AX_ORD_1"}}),
                ])),
            ):
                self.assertTrue(await exit_axisdirect(ax_pos, ax_cred))


if __name__ == "__main__":
    unittest.main()
