"""Unit tests for execution/payload_master.py order exit payloads."""

import unittest

from models.schemas import Position, Credential
from execution.payload_master import PayloadMaster


class TestExecutionPayloadMaster(unittest.TestCase):
    """Test suite for order payload creation and transaction side reversal."""

    def setUp(self):
        self.flattrade_cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            token="token_ft",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
        )
        self.alice_cred = Credential(
            broker="ALICEBLUE",
            client_id="AB12345",
            token="token_ab",
            fingerprint="ALICEBLUE:AB12345",
            status="working",
        )
        self.kotak_cred = Credential(
            broker="KOTAKNEO",
            client_id="K12345",
            token="token_kotak",
            fingerprint="KOTAKNEO:K12345",
            status="working",
        )
        self.mstock_cred = Credential(
            broker="MSTOCK",
            client_id="M123",
            token="token_mstock",
            fingerprint="MSTOCK:M123",
            status="working",
        )
        self.fyers_cred = Credential(
            broker="FYERS",
            client_id="FY123",
            token="token_fyers",
            fingerprint="FYERS:FY123",
            status="working",
        )
        self.axis_cred = Credential(
            broker="AXISDIRECT",
            client_id="AX123",
            token="token_axis",
            fingerprint="AXISDIRECT:AX123",
            status="working",
        )

    def test_flattrade_exit_payload_short_position_reversal(self):
        """Verify short position (qty=-1) in Flattrade generates BUY ('B') order with absolute qty=1."""
        pos = Position(
            id="FLATTRADE:FZ48062:WIPRO-EQ",
            broker="FLATTRADE",
            client_id="FZ48062",
            symbol="WIPRO-EQ",
            token="3787",
            exchange="NSE",
            product_type="INTRADAY",
            order_type="",
            transaction_type="SELL",
            quantity=-1,
            avg_price=193.73,
            ltp=193.30,
            pnl=0.43,
        )
        payload = PayloadMaster.build_flattrade_payload(pos, self.flattrade_cred)
        self.assertEqual(payload["trantype"], "B")  # BUY to close short
        self.assertEqual(payload["qty"], "1")  # Positive absolute quantity
        self.assertEqual(payload["prd"], "I")  # Intraday
        self.assertEqual(payload["tsym"], "WIPRO-EQ")
        self.assertEqual(payload["prctyp"], "MKT")

    def test_flattrade_exit_payload_long_position_reversal(self):
        """Verify long position (qty=10) in Flattrade generates SELL ('S') order."""
        pos = Position(
            id="FLATTRADE:FZ48062:RELIANCE-EQ",
            broker="FLATTRADE",
            client_id="FZ48062",
            symbol="RELIANCE-EQ",
            token="2885",
            exchange="NSE",
            product_type="CNC",
            order_type="",
            transaction_type="BUY",
            quantity=10,
            avg_price=2900.0,
            ltp=2910.0,
            pnl=100.0,
        )
        payload = PayloadMaster.build_flattrade_payload(pos, self.flattrade_cred)
        self.assertEqual(payload["trantype"], "S")  # SELL to close long
        self.assertEqual(payload["qty"], "10")
        self.assertEqual(payload["prd"], "C")  # Delivery/CNC

    def test_aliceblue_exit_payload_reversal(self):
        """Verify AliceBlue exit payload reverses BUY to SELL and SELL to BUY."""
        # Long position
        pos_long = Position(
            id="AB:1",
            broker="ALICEBLUE",
            client_id="AB12345",
            symbol="SBIN-EQ",
            token="3045",
            exchange="NSE",
            product_type="MIS",
            order_type="",
            transaction_type="BUY",
            quantity=50,
            avg_price=820.0,
            ltp=825.0,
            pnl=250.0,
        )
        p_long_list = PayloadMaster.build_alice_sdk_payload(pos_long, self.alice_cred)
        self.assertEqual(len(p_long_list), 1)
        self.assertEqual(p_long_list[0]["transactionType"], "SELL")
        self.assertEqual(p_long_list[0]["quantity"], 50)

        # Short position
        pos_short = Position(
            id="AB:2",
            broker="ALICEBLUE",
            client_id="AB12345",
            symbol="SBIN-EQ",
            token="3045",
            exchange="NSE",
            product_type="MIS",
            order_type="",
            transaction_type="SELL",
            quantity=-50,
            avg_price=820.0,
            ltp=815.0,
            pnl=250.0,
        )
        p_short_list = PayloadMaster.build_alice_sdk_payload(pos_short, self.alice_cred)
        self.assertEqual(len(p_short_list), 1)
        self.assertEqual(p_short_list[0]["transactionType"], "BUY")
        self.assertEqual(p_short_list[0]["quantity"], 50)

    def test_kotak_exit_payload_reversal(self):
        """Verify Kotak Neo exit payload uses 'S' for long and 'B' for short."""
        import json
        pos = Position(
            id="K:1",
            broker="KOTAKNEO",
            client_id="K12345",
            symbol="HDFCBANK-EQ",
            token="1333",
            exchange="NSE",
            product_type="MIS",
            order_type="",
            transaction_type="SELL",
            quantity=-5,
            avg_price=1650.0,
            ltp=1640.0,
            pnl=50.0,
        )
        payload = PayloadMaster.build_kotak_payload(pos)
        self.assertIn("jData", payload)
        j_data = json.loads(payload["jData"])
        self.assertEqual(j_data["tt"], "B")  # BUY to close short
        self.assertEqual(j_data["qt"], "5")  # abs qty as string
        self.assertEqual(j_data["pc"], "MIS")

    def test_fyers_exit_payload_reversal(self):
        """Verify Fyers exit payload uses side=-1 for long exit and side=1 for short exit."""
        pos_short = Position(
            id="FY:1",
            broker="FYERS",
            client_id="FY123",
            symbol="NSE:NIFTY26NOV24000CE",
            token="1234",
            exchange="NFO",
            product_type="INTRADAY",
            order_type="",
            transaction_type="SELL",
            quantity=-25,
            avg_price=150.0,
            ltp=140.0,
            pnl=250.0,
        )
        payload = PayloadMaster.build_fyers_payload(pos_short)
        self.assertEqual(payload["side"], 1)  # 1 = BUY to close short
        self.assertEqual(payload["qty"], 25)

    def test_mstock_and_axis_exit_payload_reversal(self):
        """Verify mStock and Axis Direct exit reversal."""
        pos = Position(
            id="MS:1",
            broker="MSTOCK",
            client_id="M123",
            symbol="ICICIBANK-EQ",
            token="4963",
            exchange="NSE",
            product_type="INTRADAY",
            order_type="",
            transaction_type="BUY",
            quantity=10,
            avg_price=1250.0,
            ltp=1260.0,
            pnl=100.0,
        )
        ms_p = PayloadMaster.build_mstock_payload(pos, "ICICIBANK-EQ")
        self.assertEqual(ms_p["transactiontype"], "SELL")
        self.assertEqual(ms_p["quantity"], "10")

        pos_ax = Position(
            id="AX:1",
            broker="AXISDIRECT",
            client_id="AX123",
            symbol="WIPRO",
            token="11939",
            exchange="NSE",
            product_type="INTRADAY",
            order_type="",
            transaction_type="SELL",
            quantity=-5,
            avg_price=193.0,
            ltp=190.0,
            pnl=15.0,
        )
        ax_p = PayloadMaster.build_axis_payload(pos_ax, self.axis_cred)
        self.assertEqual(ax_p["transactionType"], "BUY")
        self.assertEqual(ax_p["totalQty"], 5)

        pos_ax_long = pos_ax.model_copy(update={"quantity": 5, "transaction_type": "BUY"})
        ax_p_long = PayloadMaster.build_axis_payload(pos_ax_long, self.axis_cred)
        self.assertEqual(ax_p_long["transactionType"], "SELL")
        self.assertEqual(ax_p_long["totalQty"], 5)

        pos_ax_zero = pos_ax.model_copy(update={"quantity": 0})
        with self.assertRaises(ValueError):
            PayloadMaster.build_axis_payload(pos_ax_zero, self.axis_cred)

    def test_aliceblue_web_and_sdk_product_routing_and_defaults(self):
        """Verify AliceBlue Web/SDK align on Intraday default fallback and segment-aware product routing."""
        base_kw = dict(
            id="AB:1",
            broker="ALICEBLUE",
            client_id="AB12345",
            symbol="SBIN-EQ",
            token="3045",
            exchange="NSE",
            order_type="",
            transaction_type="BUY",
            quantity=10,
            avg_price=820.0,
            ltp=825.0,
            pnl=50.0,
        )

        # Empty/unknown product defaults to INTRADAY on both Web ('MIS') and SDK ('INTRADAY')
        pos_empty = Position(product_type="", **base_kw)
        self.assertEqual(PayloadMaster.build_alice_web_payload(pos_empty, self.alice_cred)[0]["product"], "MIS")
        self.assertEqual(PayloadMaster.build_alice_sdk_payload(pos_empty, self.alice_cred)[0]["product"], "INTRADAY")

        # Whitespace MIS / BO / CO map to Intraday consistently
        for pcode in (" mis ", "BO", "CO", "INTRADAY"):
            p = Position(product_type=pcode, **base_kw)
            self.assertEqual(PayloadMaster.build_alice_web_payload(p, self.alice_cred)[0]["product"], "MIS")
            self.assertEqual(PayloadMaster.build_alice_sdk_payload(p, self.alice_cred)[0]["product"], "INTRADAY")

        # Cash Delivery / CNC / LONGTERM & MTF
        pos_cnc = Position(product_type="CNC", **base_kw)
        self.assertEqual(PayloadMaster.build_alice_web_payload(pos_cnc, self.alice_cred)[0]["product"], "CNC")
        self.assertEqual(PayloadMaster.build_alice_sdk_payload(pos_cnc, self.alice_cred)[0]["product"], "LONGTERM")

        pos_mtf = Position(product_type="MTF", **base_kw)
        self.assertEqual(PayloadMaster.build_alice_web_payload(pos_mtf, self.alice_cred)[0]["product"], "MTF")
        self.assertEqual(PayloadMaster.build_alice_sdk_payload(pos_mtf, self.alice_cred)[0]["product"], "MTF")

        # Derivative contract (F&O) even when raw exchange arrived as NSE resolves to NFO + NRML/NORMAL
        for sym, exch_in in (("NIFTY26NOV25000CE", "NFO"), ("NIFTY 26NOV 25000 CE", "NSE")):
            pos_fo = Position(
                id=f"AB:FO:{sym}",
                broker="ALICEBLUE",
                client_id="AB12345",
                symbol=sym,
                token="99999",
                exchange=exch_in,
                product_type="NRML",
                order_type="",
                transaction_type="BUY",
                quantity=25,
                avg_price=120.0,
                ltp=125.0,
                pnl=125.0,
            )
            web_fo = PayloadMaster.build_alice_web_payload(pos_fo, self.alice_cred)[0]
            sdk_fo = PayloadMaster.build_alice_sdk_payload(pos_fo, self.alice_cred)[0]
            self.assertEqual(web_fo["exchange"], "NFO")
            self.assertEqual(web_fo["product"], "NRML")
            self.assertEqual(sdk_fo["exchange"], "NFO")
            self.assertEqual(sdk_fo["product"], "NORMAL")


if __name__ == "__main__":
    unittest.main()
