"""Unit tests for position/manager.py PositionManager normalization."""

import unittest

from position.manager import PositionManager
from tests.fixtures.mock_broker_responses import (
    FLATTRADE_POSITIONS_LONG,
    FLATTRADE_POSITIONS_SHORT,
    FLATTRADE_POSITIONS_MIXED_WITH_ZERO,
    ALICEBLUE_POSITIONS_SUCCESS,
    KOTAK_POSITIONS_SUCCESS,
    MSTOCK_POSITIONS_SUCCESS,
    FYERS_POSITIONS_SUCCESS,
    AXIS_POSITIONS_SUCCESS,
)


class TestPositionManager(unittest.TestCase):
    """Test suite for PositionManager.normalize_positions logic across brokers."""

    def test_normalize_flattrade_long_position(self):
        """Verify normal Flattrade long position normalization."""
        raw_list = FLATTRADE_POSITIONS_LONG["positionBookArr"]
        positions = PositionManager.normalize_positions(raw_list, "FLATTRADE", "FZ48062")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.id, "FLATTRADE:FZ48062:RELIANCE-EQ:NSE:INTRADAY")
        self.assertEqual(pos.symbol, "RELIANCE-EQ")
        self.assertEqual(pos.quantity, 10)
        self.assertEqual(pos.transaction_type, "BUY")
        self.assertEqual(pos.product_type, "INTRADAY")
        self.assertEqual(pos.avg_price, 2900.50)
        self.assertEqual(pos.ltp, 2915.00)
        self.assertEqual(pos.pnl, 145.00)

    def test_normalize_flattrade_short_position(self):
        """Verify Flattrade short position (negative netqty) normalizes with Side=SELL."""
        raw_list = FLATTRADE_POSITIONS_SHORT["positionBookArr"]
        positions = PositionManager.normalize_positions(raw_list, "FLATTRADE", "FZ48062")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.id, "FLATTRADE:FZ48062:WIPRO-EQ:NSE:INTRADAY")
        self.assertEqual(pos.symbol, "WIPRO-EQ")
        self.assertEqual(pos.quantity, -1)
        self.assertEqual(pos.transaction_type, "SELL")
        self.assertEqual(pos.product_type, "INTRADAY")
        self.assertEqual(pos.avg_price, 193.73)
        self.assertEqual(pos.ltp, 193.30)
        self.assertEqual(pos.pnl, 0.43)

    def test_zero_quantity_positions_filtered_out(self):
        """Verify zero quantity positions are removed while non-zero positions are kept."""
        raw_list = FLATTRADE_POSITIONS_MIXED_WITH_ZERO["positionBookArr"]
        positions = PositionManager.normalize_positions(raw_list, "FLATTRADE", "FZ48062")
        self.assertEqual(len(positions), 1)
        # TCS-EQ had netqty 0, so only INFY-EQ should remain
        self.assertEqual(positions[0].symbol, "INFY-EQ")
        self.assertEqual(positions[0].quantity, 2)
        self.assertEqual(positions[0].product_type, "CNC")

    def test_normalize_aliceblue_positions(self):
        """Verify AliceBlue schema mapping including capitalized Product field."""
        raw_list = ALICEBLUE_POSITIONS_SUCCESS["data"]
        positions = PositionManager.normalize_positions(raw_list, "ALICEBLUE", "AB12345")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.id, "ALICEBLUE:AB12345:SBIN-EQ:NSE:INTRADAY")
        self.assertEqual(pos.symbol, "SBIN-EQ")
        self.assertEqual(pos.exchange, "NSE")
        self.assertEqual(pos.product_type, "INTRADAY")
        self.assertEqual(pos.quantity, 50)
        self.assertEqual(pos.transaction_type, "BUY")
        self.assertEqual(pos.avg_price, 820.50)
        self.assertEqual(pos.ltp, 825.00)
        self.assertEqual(pos.pnl, 225.00)

    def test_normalize_kotak_cash_quantities(self):
        """Verify Kotak Neo position mapping and average price calculation from buy/sell amounts."""
        raw_list = KOTAK_POSITIONS_SUCCESS["data"]
        positions = PositionManager.normalize_positions(raw_list, "KOTAKNEO", "K12345")
        self.assertEqual(len(positions), 1)
        pos = positions[0]
        self.assertEqual(pos.symbol, "HDFCBANK-EQ")
        self.assertEqual(pos.quantity, 10)
        self.assertEqual(pos.transaction_type, "BUY")
        self.assertEqual(pos.avg_price, 1650.0)  # 16500.0 / 10
        self.assertEqual(pos.ltp, 1660.0)

    def test_normalize_mstock_and_fyers(self):
        """Verify mStock and Fyers position mapping."""
        # mStock
        m_pos = PositionManager.normalize_positions(MSTOCK_POSITIONS_SUCCESS["data"], "MSTOCK", "M123")
        self.assertEqual(len(m_pos), 1)
        self.assertEqual(m_pos[0].symbol, "ICICIBANK-EQ")
        self.assertEqual(m_pos[0].quantity, 25)

        # Fyers
        f_pos = PositionManager.normalize_positions(FYERS_POSITIONS_SUCCESS["netPositions"], "FYERS", "FY123")
        self.assertEqual(len(f_pos), 1)
        self.assertEqual(f_pos[0].quantity, 25)
        self.assertEqual(f_pos[0].exchange, "NFO")  # Option derivative mapped to NFO

    def test_derivative_exchange_segment_detection(self):
        """Verify Sensex derivative is mapped to BFO and Nifty to NFO."""
        sensex_raw = [{
            "symbol": "SENSEX26N0576000CE",
            "instrumenttype": "OPTIDX",
            "quantity": 10,
            "exchange": "BSE",
            "avg_price": 100.0,
            "ltp": 120.0,
        }]
        positions = PositionManager.normalize_positions(sensex_raw, "FLATTRADE", "FZ48062")
        self.assertEqual(positions[0].exchange, "BFO")

        nifty_raw = [{
            "symbol": "NIFTY26NOV24000PE",
            "instrumenttype": "OPTIDX",
            "quantity": 25,
            "exchange": "NSE",
            "avg_price": 50.0,
            "ltp": 60.0,
        }]
        positions = PositionManager.normalize_positions(nifty_raw, "FLATTRADE", "FZ48062")
        self.assertEqual(positions[0].exchange, "NFO")

    def test_pnl_fallback_calculation_for_short_position(self):
        """Verify fallback PnL calculation for short position when broker PnL is 0."""
        raw_short = [{
            "symbol": "TEST-EQ",
            "quantity": -10,
            "avg_price": 200.0,
            "ltp": 190.0,  # 10 pt profit on short
            "pnl": 0.0,
        }]
        positions = PositionManager.normalize_positions(raw_short, "FLATTRADE", "FZ48062")
        # Formula for short: (avg_price - ltp) * abs(qty) = (200 - 190) * 10 = +100.0
        self.assertEqual(positions[0].pnl, 100.0)

    def test_empty_and_malformed_input(self):
        """Verify PositionManager handles empty or malformed inputs without raising unhandled exceptions."""
        self.assertEqual(PositionManager.normalize_positions([], "FLATTRADE", "FZ48062"), [])
        self.assertEqual(PositionManager.normalize_positions([None, "invalid_str", 123], "FLATTRADE", "FZ48062"), [])

    def test_pos_id_distinguishes_products_and_exchanges(self):
        """Verify same symbol across distinct products or exchanges produces unique pos.id values."""
        raw_rows = [
            {"tsym": "SBIN-EQ", "exch": "NSE", "prd": "I", "netqty": 10, "netavgprc": 800.0, "ltp": 805.0},
            {"tsym": "SBIN-EQ", "exch": "NSE", "prd": "C", "netqty": 5, "netavgprc": 800.0, "ltp": 805.0},
            {"tsym": "SBIN-EQ", "exch": "BSE", "prd": "I", "netqty": 2, "netavgprc": 800.0, "ltp": 805.0},
        ]
        positions = PositionManager.normalize_positions(raw_rows, "FLATTRADE", "FZ48062")
        self.assertEqual(len(positions), 3)
        ids = {p.id for p in positions}
        self.assertEqual(
            ids,
            {
                "FLATTRADE:FZ48062:SBIN-EQ:NSE:INTRADAY",
                "FLATTRADE:FZ48062:SBIN-EQ:NSE:CNC",
                "FLATTRADE:FZ48062:SBIN-EQ:BSE:INTRADAY",
            },
        )

    def test_product_normalization_variants(self):
        """Verify Product casing variants, whitespace trimming, LONGTERM, MTF, and blank fallback."""
        raw_rows = [
            {"TradingSymbol": "A-EQ", "Exchange": "NSE", "Product": "LONGTERM", "Netqty": 1, "Price": 100, "LTP": 101},
            {"TradingSymbol": "B-EQ", "Exchange": "NSE", "Product": " MTF ", "Netqty": 1, "Price": 100, "LTP": 101},
            {"TradingSymbol": "C-EQ", "Exchange": "NSE", "PRODUCT": " mis ", "Netqty": 1, "Price": 100, "LTP": 101},
            {"TradingSymbol": "D-EQ", "Exchange": "NFO", "productType": "MARGIN", "Netqty": 25, "Price": 100, "LTP": 101},
            {"TradingSymbol": "E-EQ", "Exchange": "NSE", "Product": "", "Netqty": 1, "Price": 100, "LTP": 101},
        ]
        by_sym = {
            p.symbol: p.product_type
            for p in PositionManager.normalize_positions(raw_rows, "ALICEBLUE", "AB12345")
        }
        self.assertEqual(by_sym["A-EQ"], "CNC")
        self.assertEqual(by_sym["B-EQ"], "MTF")
        self.assertEqual(by_sym["C-EQ"], "INTRADAY")
        self.assertEqual(by_sym["D-EQ"], "NRML")
        self.assertEqual(by_sym["E-EQ"], "")

    def test_qty_float_zero_fallback_exchange_priority_and_instrument_token(self):
        """Verify netQty=0 does not fall back to quantity, float qty strings work, BSE-EQ on NSE stays NSE, and instrument_token maps."""
        raw_rows = [
            {"tsym": "CLOSED-EQ", "exch": "NSE", "netQty": 0, "quantity": 25, "avg_price": 100, "ltp": 100},
            {"symbol": "FLOAT-EQ", "exchange": "NSE", "quantity": "15.0", "avg_price": 100, "ltp": 105},
            {"symbol": "BSE-EQ", "exchange": "NSE", "quantity": 10, "instrument_token": "19585", "avg_price": 500, "ltp": 510},
        ]
        positions = PositionManager.normalize_positions(raw_rows, "MSTOCK", "M123")
        by_sym = {p.symbol: p for p in positions}
        self.assertNotIn("CLOSED-EQ", by_sym)
        self.assertEqual(by_sym["FLOAT-EQ"].quantity, 15)
        self.assertEqual(by_sym["BSE-EQ"].exchange, "NSE")
        self.assertEqual(by_sym["BSE-EQ"].token, "19585")


if __name__ == "__main__":
    unittest.main()
