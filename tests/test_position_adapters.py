"""Unit tests for position/adapters.py broker polling functions."""

import unittest
from unittest.mock import patch, AsyncMock
import aiohttp

from models.schemas import Credential
from core.engine import core_engine
from position.adapters import (
    poll_flattrade,
    poll_aliceblue,
    poll_fyers,
    poll_kotakneo,
)
from tests.fixtures.mock_broker_responses import (
    FLATTRADE_POSITIONS_SHORT,
    FLATTRADE_EMPTY_STATUS_I,
    FLATTRADE_HOLDINGS_VALID,
    FLATTRADE_HOLDINGS_EXPIRED,
    FLATTRADE_BROKER_ERROR,
    ALICEBLUE_POSITIONS_SUCCESS,
    ALICEBLUE_EC920_NO_POSITIONS,
    ALICEBLUE_AUTH_EXPIRED,
    FYERS_POSITIONS_SUCCESS,
    FYERS_AUTH_EXPIRED,
    KOTAK_POSITIONS_SUCCESS,
    KOTAK_AUTH_EXPIRED,
)
from tests.fixtures.mock_credentials import TODAY_IST_STR


class TestPositionAdapters(unittest.IsolatedAsyncioTestCase):
    """Test suite for broker polling adapters, session verification, and error classification."""

    async def asyncSetUp(self):
        core_engine.http_session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        if core_engine.http_session and not core_engine.http_session.closed:
            await core_engine.http_session.close()

    async def test_flattrade_poll_missing_token_raises_permission_error(self):
        """Verify poll_flattrade raises PermissionError immediately if token is empty."""
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            token="",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
        )
        with self.assertRaises(PermissionError):
            await poll_flattrade(cred)

    @patch("position.adapters.proxy_request")
    async def test_flattrade_poll_successful_short_position(self, mock_proxy):
        """Verify poll_flattrade successfully normalizes active short position."""
        mock_proxy.return_value = FLATTRADE_POSITIONS_SHORT
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            token="valid_token_xyz",
            static_ip="172.31.8.142",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
            token_created_at=TODAY_IST_STR,
        )
        positions = await poll_flattrade(cred)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "WIPRO-EQ")
        self.assertEqual(positions[0].quantity, -1)
        self.assertEqual(positions[0].transaction_type, "SELL")

    @patch("position.adapters.proxy_request")
    async def test_flattrade_poll_empty_book_detects_session_expired_via_holdings(self, mock_proxy):
        """Verify empty position book triggers getHoldings check and catches session expiration."""
        # 1st call is getPositonBook (returns status: I with empty array)
        # 2nd call is getHoldings (returns status: I with Session Expired)
        mock_proxy.side_effect = [FLATTRADE_EMPTY_STATUS_I, FLATTRADE_HOLDINGS_EXPIRED]
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            token="expired_token_xyz",
            static_ip="172.31.8.142",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
            token_created_at=TODAY_IST_STR,
        )
        with self.assertRaises(PermissionError) as ctx:
            await poll_flattrade(cred)
        self.assertIn("Session Expired", str(ctx.exception))

    @patch("position.adapters.proxy_request")
    async def test_flattrade_poll_empty_book_healthy_when_holdings_valid(self, mock_proxy):
        """Verify empty position book returns empty list when getHoldings confirms session is healthy."""
        mock_proxy.side_effect = [FLATTRADE_EMPTY_STATUS_I, FLATTRADE_HOLDINGS_VALID]
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            token="valid_token_xyz",
            static_ip="172.31.8.142",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
            token_created_at=TODAY_IST_STR,
        )
        positions = await poll_flattrade(cred)
        self.assertEqual(positions, [])

    @patch("position.adapters.proxy_request")
    async def test_flattrade_poll_broker_error_raises_runtime_error(self, mock_proxy):
        """Verify broker 500/E status raises RuntimeError without blacklisting."""
        mock_proxy.return_value = FLATTRADE_BROKER_ERROR
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            token="valid_token_xyz",
            static_ip="172.31.8.142",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
        )
        with self.assertRaises(RuntimeError):
            await poll_flattrade(cred)

    @patch("aiohttp.ClientSession.get")
    async def test_aliceblue_poll_ec920_returns_empty_positions(self, mock_get):
        """Verify AliceBlue EC920 code is recognized as healthy 0-positions, not auth error."""
        from tests.helpers import MockHttpResponse
        mock_get.return_value = MockHttpResponse(ALICEBLUE_EC920_NO_POSITIONS, status=200)

        cred = Credential(
            broker="ALICEBLUE",
            client_id="AB12345",
            token="valid_token",
            fingerprint="ALICEBLUE:AB12345",
            status="working",
        )
        positions = await poll_aliceblue(cred)
        self.assertEqual(positions, [])

    @patch("aiohttp.ClientSession.get")
    async def test_aliceblue_poll_auth_expired_raises_permission_error(self, mock_get):
        """Verify AliceBlue session expiration raises PermissionError."""
        from tests.helpers import MockHttpResponse
        mock_get.return_value = MockHttpResponse(ALICEBLUE_AUTH_EXPIRED, status=200)

        cred = Credential(
            broker="ALICEBLUE",
            client_id="AB12345",
            token="expired_token",
            fingerprint="ALICEBLUE:AB12345",
            status="working",
        )
        with self.assertRaises(PermissionError):
            await poll_aliceblue(cred)

    @patch("position.adapters.proxy_request")
    async def test_fyers_poll_auth_expired_raises_permission_error(self, mock_proxy):
        """Verify Fyers 401 error response raises PermissionError."""
        mock_proxy.return_value = FYERS_AUTH_EXPIRED
        cred = Credential(
            broker="FYERS",
            client_id="FY123",
            token="expired_token",
            static_ip="172.31.8.142",
            fingerprint="FYERS:FY123",
            status="working",
        )
        with self.assertRaises(PermissionError):
            await poll_fyers(cred)


if __name__ == "__main__":
    unittest.main()
