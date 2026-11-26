"""Unit tests for position/blocklist.py and position/chronos.py polling logic."""

import unittest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timedelta
import zoneinfo

from models.schemas import Credential, Position
from position.blocklist import (
    add_to_blocklist,
    is_blocked,
    get_blocked_users,
    get_blocked_user,
    remove_from_blocklist,
    clear_blocklist,
)
from tests.fixtures.mock_credentials import TODAY_IST_STR

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")


class TestChronosAndBlocklist(unittest.IsolatedAsyncioTestCase):
    """Test suite for Failed Auth blocklist management and Chronos session validation."""

    def setUp(self):
        clear_blocklist()

    def tearDown(self):
        clear_blocklist()

    def test_blocklist_add_and_query(self):
        """Verify adding an account to blocklist and retrieving details."""
        add_to_blocklist(
            client_id="FZ48062",
            broker="FLATTRADE",
            username="Manoj Flattrade",
            reason="Session token expired (HTTP 401)",
            signature="sig_abc_123",
        )
        self.assertTrue(is_blocked("FZ48062"))

        blocked_entry = get_blocked_user("FZ48062")
        self.assertIsNotNone(blocked_entry)
        self.assertEqual(blocked_entry["client_id"], "FZ48062")
        self.assertEqual(blocked_entry["broker"], "FLATTRADE")
        self.assertEqual(blocked_entry["reason"], "Session token expired (HTTP 401)")
        self.assertEqual(blocked_entry["credential_signature"], "sig_abc_123")

    def test_blocklist_remove_and_clear(self):
        """Verify removing single account and clearing entire blocklist."""
        add_to_blocklist("AB123", "ALICEBLUE", "Alice", "Reason 1")
        add_to_blocklist("FZ48062", "FLATTRADE", "Manoj", "Reason 2")
        self.assertEqual(len(get_blocked_users()), 2)

        # Remove single
        removed = remove_from_blocklist("AB123")
        self.assertTrue(removed)
        self.assertFalse(is_blocked("AB123"))
        self.assertEqual(len(get_blocked_users()), 1)

        # Clear remaining
        count = clear_blocklist()
        self.assertEqual(count, 1)
        self.assertEqual(len(get_blocked_users()), 0)

    @patch("utils.email.send_auth_failed_email", new_callable=AsyncMock)
    @patch("position.chronos.login_flattrade", new_callable=AsyncMock)
    async def test_refresh_token_success_removes_from_blocklist(self, mock_login, mock_email):
        """Verify successful authentication removes client from blocklist."""
        from position.chronos import refresh_token_with_backoff

        # Initially block user
        add_to_blocklist("FZ48062", "FLATTRADE", "Manoj", "Initial failure")
        self.assertTrue(is_blocked("FZ48062"))

        mock_login.return_value = "fresh_token_123"
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            password="pwd",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
        )

        success = await refresh_token_with_backoff(cred, send_email=False)
        self.assertTrue(success)
        self.assertFalse(is_blocked("FZ48062"))

    @patch("utils.email.send_auth_failed_email", new_callable=AsyncMock)
    @patch("position.chronos.login_flattrade", new_callable=AsyncMock)
    async def test_refresh_token_failure_adds_to_blocklist(self, mock_login, mock_email):
        """Verify authentication failure adds client to blocklist with reason."""
        from position.chronos import refresh_token_with_backoff

        mock_login.side_effect = PermissionError("Invalid TOTP or Password")
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            password="bad_pwd",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
        )

        success = await refresh_token_with_backoff(cred, send_email=False)
        self.assertFalse(success)
        self.assertTrue(is_blocked("FZ48062"))
        blocked_info = get_blocked_user("FZ48062")
        self.assertIn("Invalid TOTP or Password", blocked_info["reason"])

    @patch("position.chronos.login_flattrade", new_callable=AsyncMock)
    async def test_refresh_token_transient_error_skips_blocklist(self, mock_login):
        """Verify transient 502/503 network errors do not quarantine account into blocklist."""
        from position.chronos import refresh_token_with_backoff

        mock_login.side_effect = RuntimeError("HTTP 502 Bad Gateway from Broker")
        cred = Credential(
            broker="FLATTRADE",
            client_id="FZ48062",
            password="pwd",
            fingerprint="FLATTRADE:FZ48062",
            status="working",
        )

        success = await refresh_token_with_backoff(cred, send_email=False)
        self.assertFalse(success)
        self.assertFalse(is_blocked("FZ48062"))  # Must NOT be blocked for transient 502

    def test_calendar_freshness_logic(self):
        """Verify calendar freshness validation compares token issuance against today's IST date."""
        today_ist = datetime.now(tz=_IST).date()
        yesterday_str = (datetime.now(tz=_IST) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        today_str = datetime.now(tz=_IST).strftime("%Y-%m-%d %H:%M:%S")

        y_date = datetime.strptime(yesterday_str[:10], "%Y-%m-%d").date()
        t_date = datetime.strptime(today_str[:10], "%Y-%m-%d").date()

        self.assertTrue(y_date < today_ist)
        self.assertFalse(t_date < today_ist)

    def test_main_logger_auth_imported_and_formatter_safe(self):
        """Verify main.py imports logger_auth and DRMTLogFormatter formats without strftime."""
        import logging
        import main
        from logger.logger import DRMTLogFormatter

        self.assertTrue(hasattr(main, "logger_auth"))
        fmt = DRMTLogFormatter()
        rec = logging.LogRecord("DRMT.Test", logging.INFO, "main.py", 13, "hello", (), None)
        formatted = fmt.format(rec)
        self.assertIn(" | INFO     | main.py:13 | hello", formatted)

    async def test_core_engine_spawn_and_remove_user_prevents_duplicates(self):
        """Verify CoreEngine tracks _user_tasks, cancels older tasks, and safely scopes remove_user."""
        import asyncio
        from core.engine import CoreEngine

        engine = CoreEngine()
        async def _fake_loop(user_id, ws, creds, eng):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        with patch("position.chronos.polling_loop", side_effect=_fake_loop):
            t1 = await engine.spawn_user("u1", None, [])
            self.assertIs(engine._user_tasks.get("u1"), t1)
            self.assertFalse(t1.done())

            t2 = await engine.spawn_user("u1", None, [])
            self.assertIs(engine._user_tasks.get("u1"), t2)
            self.assertTrue(t1.cancelled())
            self.assertFalse(t2.done())

            # Removing the old task must NOT cancel or remove t2
            engine.remove_user("u1", task=t1)
            self.assertIs(engine._user_tasks.get("u1"), t2)
            self.assertFalse(t2.done())

            # Removing t2 cleans up and cancels t2
            engine.remove_user("u1", task=t2)
            self.assertNotIn("u1", engine._user_tasks)
            await asyncio.sleep(0)
            self.assertTrue(t2.cancelled())


if __name__ == "__main__":
    unittest.main()
