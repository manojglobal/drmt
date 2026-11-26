"""Unit tests for Failed Auth error classification, zero-position safety, and quarantine across all 6 brokers."""

import unittest
from unittest.mock import patch, AsyncMock
import aiohttp

from models.schemas import Credential
from core.engine import core_engine
from position.blocklist import add_to_blocklist, remove_from_blocklist, is_blocked, clear_blocklist, get_blocked_users
from position.adapters import (
    poll_flattrade,
    poll_mstock,
    poll_aliceblue,
    poll_fyers,
    poll_kotakneo,
    poll_axisdirect,
)
from tests.fixtures.mock_broker_responses import (
    FLATTRADE_POSITIONS_SHORT,
    FLATTRADE_EMPTY_STATUS_I,
    FLATTRADE_HOLDINGS_VALID,
    FLATTRADE_HOLDINGS_EXPIRED,
    MSTOCK_POSITIONS_SUCCESS,
    MSTOCK_EMPTY_SUCCESS,
    MSTOCK_AUTH_EXPIRED,
    KOTAK_POSITIONS_SUCCESS,
    KOTAK_NO_DATA,
    KOTAK_STCODE_100022,
    AXIS_POSITIONS_SUCCESS,
    AXIS_EMPTY_SUCCESS,
    AXIS_AUTH_EXPIRED_1004,
    FYERS_POSITIONS_SUCCESS,
    FYERS_EMPTY_SUCCESS,
    FYERS_AUTH_EXPIRED_MINUS_17,
    ALICEBLUE_POSITIONS_SUCCESS,
    ALICEBLUE_EC920_NO_POSITIONS,
    ALICEBLUE_AUTH_EXPIRED,
)
from tests.fixtures.mock_credentials import TODAY_IST_STR


class TestFailedAuthAdapters(unittest.IsolatedAsyncioTestCase):
    """Test suite validating accurate auth error classification without false positives."""

    async def asyncSetUp(self):
        clear_blocklist()
        core_engine.http_session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        clear_blocklist()
        if core_engine.http_session and not core_engine.http_session.closed:
            await core_engine.http_session.close()

    # =========================================================================
    # 1. FLATTRADE
    # =========================================================================
    @patch("position.adapters.proxy_request")
    async def test_flattrade_normal_positions_success(self, mock_proxy):
        """FLATTRADE: Valid account with open positions normalizes correctly."""
        mock_proxy.return_value = FLATTRADE_POSITIONS_SHORT
        cred = Credential(
            broker="FLATTRADE", client_id="FZ48062", token="valid_tok",
            static_ip="172.31.8.142", fingerprint="FLATTRADE:FZ48062", status="working",
            token_created_at=TODAY_IST_STR
        )
        positions = await poll_flattrade(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "WIPRO-EQ")
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_flattrade_zero_positions_not_blocked(self, mock_proxy):
        """FLATTRADE: Zero positions (status=I with valid holdings) returns [] without blocking."""
        mock_proxy.side_effect = [FLATTRADE_EMPTY_STATUS_I, FLATTRADE_HOLDINGS_VALID]
        cred = Credential(
            broker="FLATTRADE", client_id="FZ48062", token="valid_tok",
            static_ip="172.31.8.142", fingerprint="FLATTRADE:FZ48062", status="working",
            token_created_at=TODAY_IST_STR
        )
        positions = await poll_flattrade(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_flattrade_expired_token_quarantined(self, mock_proxy):
        """FLATTRADE: Expired session token raises PermissionError."""
        mock_proxy.side_effect = [FLATTRADE_EMPTY_STATUS_I, FLATTRADE_HOLDINGS_EXPIRED]
        cred = Credential(
            broker="FLATTRADE", client_id="FZ48062", token="expired_tok",
            static_ip="172.31.8.142", fingerprint="FLATTRADE:FZ48062", status="working",
            token_created_at=TODAY_IST_STR
        )
        with self.assertRaises(PermissionError):
            await poll_flattrade(cred)

    # =========================================================================
    # 2. MSTOCK
    # =========================================================================
    @patch("position.adapters.proxy_request")
    async def test_mstock_normal_positions_success(self, mock_proxy):
        """MSTOCK: Valid positions payload normalizes correctly."""
        mock_proxy.return_value = MSTOCK_POSITIONS_SUCCESS
        cred = Credential(
            broker="MSTOCK", client_id="MS12345", token="valid_tok",
            static_ip="172.31.8.142", fingerprint="MSTOCK:MS12345", status="working"
        )
        positions = await poll_mstock(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "ICICIBANK-EQ")
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_mstock_zero_positions_not_blocked(self, mock_proxy):
        """MSTOCK: Healthy account with 0 positions (data=[]) returns [] without error."""
        mock_proxy.return_value = MSTOCK_EMPTY_SUCCESS
        cred = Credential(
            broker="MSTOCK", client_id="MS12345", token="valid_tok",
            static_ip="172.31.8.142", fingerprint="MSTOCK:MS12345", status="working"
        )
        positions = await poll_mstock(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_mstock_invalid_token_quarantined(self, mock_proxy):
        """MSTOCK: Invalid/Expired JWT raises PermissionError."""
        mock_proxy.return_value = MSTOCK_AUTH_EXPIRED
        cred = Credential(
            broker="MSTOCK", client_id="MS12345", token="expired_tok",
            static_ip="172.31.8.142", fingerprint="MSTOCK:MS12345", status="working"
        )
        with self.assertRaises(PermissionError) as ctx:
            await poll_mstock(cred)
        self.assertIn("Invalid JWT Token", str(ctx.exception))

    # =========================================================================
    # 3. KOTAK NEO
    # =========================================================================
    @patch("position.adapters.proxy_request")
    async def test_kotakneo_normal_positions_success(self, mock_proxy):
        """KOTAK NEO: Valid position book normalizes correctly."""
        mock_proxy.return_value = KOTAK_POSITIONS_SUCCESS
        cred = Credential(
            broker="KOTAK NEO", client_id="KOTAK123", token="valid_tok",
            api_key="api_token_val", sid="session_sid",
            static_ip="172.31.8.142", fingerprint="KOTAK:KOTAK123", status="working"
        )
        positions = await poll_kotakneo(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "HDFCBANK-EQ")
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_kotakneo_zero_positions_not_blocked(self, mock_proxy):
        """KOTAK NEO: No data available response returns [] without blocking."""
        mock_proxy.return_value = KOTAK_NO_DATA
        cred = Credential(
            broker="KOTAK NEO", client_id="KOTAK123", token="valid_tok",
            api_key="api_token_val", sid="session_sid",
            static_ip="172.31.8.142", fingerprint="KOTAK:KOTAK123", status="working"
        )
        positions = await poll_kotakneo(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_kotakneo_stcode_100022_quarantined(self, mock_proxy):
        """KOTAK NEO: stCode=100022 (Token Expired) raises PermissionError."""
        mock_proxy.return_value = KOTAK_STCODE_100022
        cred = Credential(
            broker="KOTAK NEO", client_id="KOTAK123", token="expired_tok",
            api_key="api_token_val", sid="session_sid",
            static_ip="172.31.8.142", fingerprint="KOTAK:KOTAK123", status="working"
        )
        with self.assertRaises(PermissionError) as ctx:
            await poll_kotakneo(cred)
        self.assertIn("100022", str(ctx.exception))

    # =========================================================================
    # 4. AXIS DIRECT
    # =========================================================================
    @patch("position.adapters.send_axis_api_request")
    @patch("position.adapters.get_axis_public_key", new_callable=AsyncMock)
    async def test_axisdirect_normal_positions_success(self, mock_pk, mock_send):
        """AXIS DIRECT: Valid open positions normalize correctly."""
        mock_pk.return_value = "mock_public_key"
        mock_send.return_value = (200, AXIS_POSITIONS_SUCCESS)
        cred = Credential(
            broker="AXIS DIRECT", client_id="AXIS123", token="valid_tok",
            api_key="client_id_val", api_secret="auth_key_val", public_key="pk_val",
            static_ip="172.31.8.142", fingerprint="AXIS:AXIS123", status="working"
        )
        positions = await poll_axisdirect(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "11939")
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.send_axis_api_request")
    @patch("position.adapters.get_axis_public_key", new_callable=AsyncMock)
    async def test_axisdirect_zero_positions_not_blocked(self, mock_pk, mock_send):
        """AXIS DIRECT: Empty positions returns [] without calling get-order-book or blocking."""
        mock_pk.return_value = "mock_public_key"
        mock_send.return_value = (200, AXIS_EMPTY_SUCCESS)
        cred = Credential(
            broker="AXIS DIRECT", client_id="AXIS123", token="valid_tok",
            api_key="client_id_val", api_secret="auth_key_val", public_key="pk_val",
            static_ip="172.31.8.142", fingerprint="AXIS:AXIS123", status="working"
        )
        positions = await poll_axisdirect(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))
        called_urls = [c.kwargs["url"] for c in mock_send.call_args_list]
        self.assertEqual(len(called_urls), 2)
        self.assertTrue(all(u.endswith("/trading/portfolio/get-positions") for u in called_urls))
        self.assertFalse(any("get-order-book" in u for u in called_urls))

    @patch("position.adapters.send_axis_api_request")
    @patch("position.adapters.get_axis_public_key", new_callable=AsyncMock)
    async def test_axisdirect_validation_error_1001_not_auth_error(self, mock_pk, mock_send):
        """AXIS DIRECT: API validation code 1001 returns [] without falsely raising PermissionError."""
        mock_pk.return_value = "mock_public_key"
        mock_send.return_value = (200, {
            "statusCode": 400,
            "status": "Bad Request",
            "error": {"code": "1001", "error": "failed validation for type", "field": "type", "value": "open"},
        })
        cred = Credential(
            broker="AXIS DIRECT", client_id="AXIS123", token="valid_tok",
            api_key="client_id_val", api_secret="auth_key_val", public_key="pk_val",
            static_ip="172.31.8.142", fingerprint="AXIS:AXIS123", status="working"
        )
        positions = await poll_axisdirect(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.send_axis_api_request")
    @patch("position.adapters.get_axis_public_key", new_callable=AsyncMock)
    async def test_axisdirect_code_1004_quarantined(self, mock_pk, mock_send):
        """AXIS DIRECT: Error code 1004 (Token Expired) raises PermissionError."""
        mock_pk.return_value = "mock_public_key"
        mock_send.return_value = (200, AXIS_AUTH_EXPIRED_1004)
        cred = Credential(
            broker="AXIS DIRECT", client_id="AXIS123", token="expired_tok",
            api_key="client_id_val", api_secret="auth_key_val", public_key="pk_val",
            static_ip="172.31.8.142", fingerprint="AXIS:AXIS123", status="working"
        )
        with self.assertRaises(PermissionError) as ctx:
            await poll_axisdirect(cred)
        self.assertIn("1004", str(ctx.exception))

    # =========================================================================
    # 5. FYERS
    # =========================================================================
    @patch("position.adapters.proxy_request")
    async def test_fyers_normal_positions_success(self, mock_proxy):
        """FYERS: Valid netPositions array normalizes correctly."""
        mock_proxy.return_value = FYERS_POSITIONS_SUCCESS
        cred = Credential(
            broker="FYERS", client_id="FY123", token="valid_tok",
            api_key="app_id", static_ip="172.31.8.142",
            fingerprint="FYERS:FY123", status="working"
        )
        positions = await poll_fyers(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "NSE:NIFTY26NOV24000CE")
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_fyers_zero_positions_not_blocked(self, mock_proxy):
        """FYERS: Empty netPositions array returns [] without blocking."""
        mock_proxy.return_value = FYERS_EMPTY_SUCCESS
        cred = Credential(
            broker="FYERS", client_id="FY123", token="valid_tok",
            api_key="app_id", static_ip="172.31.8.142",
            fingerprint="FYERS:FY123", status="working"
        )
        positions = await poll_fyers(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))

    @patch("position.adapters.proxy_request")
    async def test_fyers_code_minus_17_quarantined(self, mock_proxy):
        """FYERS: Auth code -17 raises PermissionError."""
        mock_proxy.return_value = FYERS_AUTH_EXPIRED_MINUS_17
        cred = Credential(
            broker="FYERS", client_id="FY123", token="expired_tok",
            api_key="app_id", static_ip="172.31.8.142",
            fingerprint="FYERS:FY123", status="working"
        )
        with self.assertRaises(PermissionError) as ctx:
            await poll_fyers(cred)
        self.assertIn("-17", str(ctx.exception))

    # =========================================================================
    # 6. ALICE BLUE
    # =========================================================================
    @patch("aiohttp.ClientSession.get")
    async def test_aliceblue_normal_positions_success(self, mock_get):
        """ALICEBLUE: Valid result positions array normalizes correctly."""
        from tests.helpers import MockHttpResponse
        mock_get.return_value = MockHttpResponse(ALICEBLUE_POSITIONS_SUCCESS, status=200)
        cred = Credential(
            broker="ALICEBLUE", client_id="AB12345", token="valid_tok",
            fingerprint="ALICEBLUE:AB12345", status="working"
        )
        positions = await poll_aliceblue(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "SBIN-EQ")
        self.assertFalse(is_blocked(cred.client_id))

    @patch("aiohttp.ClientSession.get")
    async def test_aliceblue_ec920_zero_positions_not_blocked(self, mock_get):
        """ALICEBLUE: EC920 'No open positions' returns [] without blocking."""
        from tests.helpers import MockHttpResponse
        mock_get.return_value = MockHttpResponse(ALICEBLUE_EC920_NO_POSITIONS, status=200)
        cred = Credential(
            broker="ALICEBLUE", client_id="AB12345", token="valid_tok",
            fingerprint="ALICEBLUE:AB12345", status="working"
        )
        positions = await poll_aliceblue(cred)
        self.assertEqual(positions, [])
        self.assertFalse(is_blocked(cred.client_id))

    @patch("aiohttp.ClientSession.get")
    async def test_aliceblue_session_expired_quarantined(self, mock_get):
        """ALICEBLUE: Session token expired message raises PermissionError."""
        from tests.helpers import MockHttpResponse
        mock_get.return_value = MockHttpResponse(ALICEBLUE_AUTH_EXPIRED, status=200)
        cred = Credential(
            broker="ALICEBLUE", client_id="AB12345", token="expired_tok",
            fingerprint="ALICEBLUE:AB12345", status="working"
        )
        with self.assertRaises(PermissionError) as ctx:
            await poll_aliceblue(cred)
        self.assertIn("expired", str(ctx.exception).lower())

    # =========================================================================
    # 7. CHRONOS EXCEPTION ROUTING & TRANSIENT ERROR FILTER
    # =========================================================================
    async def test_chronos_refresh_token_transient_vs_auth(self):
        """Verify refresh_token_with_backoff correctly separates network timeouts from session timeouts."""
        from position.chronos import refresh_token_with_backoff

        cred = Credential(
            broker="MSTOCK", client_id="TEST_CID", token="tok",
            fingerprint="MSTOCK:TEST_CID", status="working"
        )

        # 1. Real network exception -> transient, skips blocklist
        with patch("position.chronos.login_mstock", side_effect=ConnectionRefusedError("Connection refused")):
            ok = await refresh_token_with_backoff(cred, send_email=False)
            self.assertFalse(ok)
            self.assertFalse(is_blocked(cred.client_id))

        # 2. Broker Session error -> genuine auth failure, added to blocklist
        with patch("position.chronos.login_mstock", side_effect=ValueError("Session timed out: Invalid password or token")):
            ok = await refresh_token_with_backoff(cred, send_email=False)
            self.assertFalse(ok)
            self.assertTrue(is_blocked(cred.client_id))

        # 3. Infra 'Core Engine HTTP session not initialized' and HTTP 503 must NOT falsely blocklist
        clear_blocklist()
        with patch("position.chronos.login_mstock", side_effect=RuntimeError("Core Engine HTTP session not initialized")):
            ok = await refresh_token_with_backoff(cred, send_email=False)
            self.assertFalse(ok)
            self.assertFalse(is_blocked(cred.client_id))

        with patch("position.chronos.login_mstock", side_effect=RuntimeError("Broker HTTP 503 Error: Service Unavailable - session retry")):
            ok = await refresh_token_with_backoff(cred, send_email=False)
            self.assertFalse(ok)
            self.assertFalse(is_blocked(cred.client_id))


if __name__ == "__main__":
    unittest.main()
