"""Unit tests for morning/market-close/Axis schedulers and 90s blocked credentials watcher."""

import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
import zoneinfo

from credentials.dynamic_loader import compute_credential_signature
from credentials.scheduler import (
    build_refresh_tables_html,
    execute_market_close_email_report,
    execute_morning_credential_refresh,
    get_seconds_until_next_ist_time,
    is_before_daily_refresh,
    is_morning_refresh_running,
    refresh_all_axis_direct_tokens,
)
from credentials.watcher import (
    get_seconds_until_next_market_session,
    is_market_session_active,
    run_blocked_credentials_check_iteration,
)
from database.db import get_db
from position.blocklist import add_to_blocklist, clear_blocklist, is_blocked
from tests.helpers import isolated_test_db, suppress_email_notifications

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")


class TestSchedulersAndBlockedWatcher(unittest.IsolatedAsyncioTestCase):
    """Tests IST scheduler math, morning refresh emails, market-close stats, and blocked watcher recovery."""

    def setUp(self):
        clear_blocklist()

    def tearDown(self):
        clear_blocklist()

    def test_market_session_and_ist_schedule_calculations(self):
        """Verify market-hours and next-session timing calculations on weekdays and weekends."""
        # Wednesday 10:30 IST -> active market session
        wed_open = datetime(2026, 11, 18, 10, 30, 0, tzinfo=_IST)
        self.assertTrue(is_market_session_active(wed_open))
        self.assertEqual(get_seconds_until_next_market_session(wed_open), 0.0)

        # Wednesday 09:00 IST -> pre-market (15 min = 900s to 09:15)
        wed_pre = datetime(2026, 11, 18, 9, 0, 0, tzinfo=_IST)
        self.assertFalse(is_market_session_active(wed_pre))
        self.assertAlmostEqual(get_seconds_until_next_market_session(wed_pre), 900.0, delta=1.0)

        # Saturday 11:00 IST -> weekend (waits until Monday 09:15 IST)
        sat = datetime(2026, 11, 21, 11, 0, 0, tzinfo=_IST)
        self.assertFalse(is_market_session_active(sat))
        self.assertGreater(get_seconds_until_next_market_session(sat), 36 * 3600)

        # Scheduler time helper always returns positive seconds
        self.assertGreaterEqual(get_seconds_until_next_ist_time(9, 10), 1.0)

        # Pre-09:10 IST boundary check
        before_0910 = datetime(2026, 11, 18, 9, 9, 59, tzinfo=_IST)
        exact_0910 = datetime(2026, 11, 18, 9, 10, 0, tzinfo=_IST)
        after_0910 = datetime(2026, 11, 18, 9, 15, 0, tzinfo=_IST)
        self.assertTrue(is_before_daily_refresh(before_0910))
        self.assertFalse(is_before_daily_refresh(exact_0910))
        self.assertFalse(is_before_daily_refresh(after_0910))

    async def test_execute_morning_credential_refresh_sends_tabular_email_with_nonempty_emails(self):
        """Morning refresh syncs CSV, clears prior blocklist, runs auth, and emails status table."""
        from credentials.flattrade import FLATTRADE_MISSING_KEYS_MESSAGE

        add_to_blocklist("OLD_BLK", "MSTOCK", "Old User", "Old reason")
        auth_results = [
            {"broker": "FLATTRADE", "client_id": "FZ59716", "username": "Jagdeesh Sawant", "user_email": "jagdeesh@gmail.com", "refreshed": True, "reason": ""},
            {"broker": "MSTOCK", "client_id": "P645M1", "username": "Manoj", "user_email": "manoj@gmail.com", "refreshed": False, "reason": "Wrong TOTP"},
            {"broker": "FLATTRADE", "client_id": "FZ58776", "username": "Sonali", "user_email": "sonali@gmail.com", "refreshed": False, "reason": FLATTRADE_MISSING_KEYS_MESSAGE},
        ]

        plain, html_out = build_refresh_tables_html(auth_results)
        self.assertIn("jagdeesh@gmail.com", plain)
        self.assertIn("Wrong TOTP", html_out)
        self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, html_out)
        self.assertIn("Flattrade Access Token Status", html_out)

        running_during_auth = []

        async def _fake_auth():
            running_during_auth.append(is_morning_refresh_running())
            return (
                ["FLATTRADE:FZ59716"],
                [("MSTOCK:P645M1", "Wrong TOTP"), ("FLATTRADE:FZ58776", FLATTRADE_MISSING_KEYS_MESSAGE)],
                auth_results,
            )

        mock_err_email = AsyncMock()
        mock_ok_email = AsyncMock()
        with (
            patch("credentials.dynamic_loader.fetch_and_sync_credentials", new=AsyncMock(return_value=3)),
            patch("main.authenticate_configured_credentials", side_effect=_fake_auth),
            patch("utils.email.send_error_email", mock_err_email),
            patch("utils.email.send_email_notification", mock_ok_email),
        ):
            await execute_morning_credential_refresh()

        self.assertFalse(is_blocked("OLD_BLK"))
        self.assertEqual(running_during_auth, [True])
        self.assertFalse(is_morning_refresh_running())
        mock_err_email.assert_awaited_once()
        mock_ok_email.assert_not_awaited()
        sent_plain = mock_err_email.await_args.args[1]
        self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, sent_plain)
        self.assertIn("sonali@gmail.com", sent_plain)

    async def test_execute_market_close_report_no_email_dispatched(self):
        """Market-close report computes stats without sending emails (Issue 23)."""
        async with isolated_test_db(), suppress_email_notifications() as mock_mail:
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token)
                    VALUES ('FLATTRADE', 'FZ59716', 'k', 's', 'FLATTRADE:FZ59716', 'working', 'tok_1'),
                           ('MSTOCK', 'P645M9', 'k2', 's2', 'MSTOCK:P645M9', 'working', '')
                    """
                )
                await db.commit()

            add_to_blocklist("P645M9", "MSTOCK", "Deepali Mehta", "Token expired")
            with (
                patch("utils.user_mapping.get_configured_client_ids", return_value={"FZ59716", "P645M9"}),
                patch("utils.user_mapping.get_client_username", side_effect=lambda cid: cid),
                patch("credentials.dynamic_loader.get_client_email", side_effect=lambda cid: f"{cid}@gmail.com"),
            ):
                stats = await execute_market_close_email_report()

        self.assertEqual(stats["successful_count"], 1)
        self.assertEqual(stats["failed_count"], 1)
        self.assertEqual(stats["total_count"], 2)
        mock_mail.assert_not_awaited()

    async def test_refresh_all_axis_direct_tokens(self):
        """13-minute worker refreshes only working configured Axis Direct accounts."""
        async with isolated_test_db():
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token)
                    VALUES ('AXIS DIRECT', '11939', 'ak', 'as', 'AXIS DIRECT:11939', 'working', 't_ax'),
                           ('FLATTRADE', 'FZ59716', 'fk', 'fs', 'FLATTRADE:FZ59716', 'working', 't_ft')
                    """
                )
                await db.commit()

            with (
                patch("credentials.dynamic_loader.get_dynamic_configured_cids", return_value={"11939", "FZ59716"}),
                patch("credentials.dynamic_loader.get_dynamic_credentials", return_value={}),
                patch("credentials.axisdirect.refresh_user_axis_direct_access_token", new=AsyncMock(return_value=True)) as mock_ax,
            ):
                refreshed = await refresh_all_axis_direct_tokens()

        self.assertEqual(refreshed, ["11939"])
        mock_ax.assert_awaited_once()

    async def test_blocked_watcher_skips_unchanged_and_unblocks_on_changed_credentials(self):
        """Watcher skips broker auth when signature is unchanged, and unblocks when CSV signature changes."""
        old_cred = {"broker": "FLATTRADE", "client_id": "FZ48062", "password": "old_password", "totp_secret": "TOTP1"}
        new_cred = {"broker": "FLATTRADE", "client_id": "FZ48062", "password": "new_password", "totp_secret": "TOTP1"}
        old_sig = compute_credential_signature(old_cred)

        add_to_blocklist("FZ48062", "FLATTRADE", "Manoj Flattrade", "Wrong Password", signature=old_sig)

        # 1. Signature unchanged -> 0 broker auth calls
        with (
            patch("credentials.watcher.fetch_and_sync_credentials", new=AsyncMock(return_value=1)),
            patch("credentials.watcher.get_dynamic_credentials", return_value=old_cred),
            patch("credentials.watcher.refresh_token_with_backoff", new=AsyncMock()) as mock_refresh,
        ):
            unblocked = await run_blocked_credentials_check_iteration()
        self.assertEqual(unblocked, 0)
        mock_refresh.assert_not_awaited()
        self.assertTrue(is_blocked("FZ48062"))

        # 2. Operator updates password -> signature changes -> authenticates & unblocks
        with (
            patch("credentials.watcher.fetch_and_sync_credentials", new=AsyncMock(return_value=1)),
            patch("credentials.watcher.get_dynamic_credentials", return_value=new_cred),
            patch("credentials.watcher.get_client_email", return_value="manoj@gmail.com"),
            patch("credentials.watcher.refresh_token_with_backoff", new=AsyncMock(return_value=True)),
            patch("credentials.watcher.send_credentials_restored_email", new=AsyncMock()),
        ):
            unblocked2 = await run_blocked_credentials_check_iteration()
        self.assertEqual(unblocked2, 1)
        self.assertFalse(is_blocked("FZ48062"))

    async def test_refresh_table_and_watcher_merge_flattrade_missing_keys_into_single_email(self):
        """Refresh table maps SDK missing-key errors to clean text and watcher sends 1 merged email for multiple failures."""
        import asyncio
        from credentials.flattrade import FLATTRADE_MISSING_KEYS_MESSAGE, reset_missing_keys_alert_cache
        from position.blocklist import get_blocked_user

        reset_missing_keys_alert_cache()
        plain_tbl, html_tbl = build_refresh_tables_html([
            {
                "broker": "FLATTRADE",
                "client_id": "FZ58776",
                "username": "Sonali Jadhav",
                "user_email": "sonali@gmail.com",
                "refreshed": False,
                "reason": "Flattrade SDK mode is active, but client FZ58776 is missing 'api_key' and 'api_secret'.",
            }
        ])
        self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, plain_tbl)
        self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, html_tbl)

        add_to_blocklist("FZ58776", "FLATTRADE", "Sonali Jadhav", "Token expired", signature="old1")
        add_to_blocklist("FZ48062", "FLATTRADE", "Manoj Flattrade", "Token expired", signature="old2")

        updated_map = {
            "FZ58776": {"broker": "FLATTRADE", "client_id": "FZ58776", "password": "p1", "api_key": "", "api_secret": ""},
            "FZ48062": {"broker": "FLATTRADE", "client_id": "FZ48062", "password": "p2", "api_key": "", "api_secret": ""},
        }

        mock_send = AsyncMock()
        with (
            patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"),
            patch("credentials.watcher.fetch_and_sync_credentials", new=AsyncMock(return_value=2)),
            patch("credentials.watcher.get_dynamic_credentials", side_effect=lambda cid: updated_map.get(cid)),
            patch("credentials.watcher.get_client_email", side_effect=lambda cid: f"{cid.lower()}@gmail.com"),
            patch("credentials.watcher.refresh_token_with_backoff", new=AsyncMock(return_value=False)),
            patch("utils.email.send_error_email", mock_send),
        ):
            # Update reason on blocklist first to simulate missing-key failure reason
            get_blocked_user("FZ58776")["reason"] = FLATTRADE_MISSING_KEYS_MESSAGE
            get_blocked_user("FZ48062")["reason"] = FLATTRADE_MISSING_KEYS_MESSAGE
            await run_blocked_credentials_check_iteration()
            await asyncio.sleep(0)

        self.assertEqual(mock_send.await_count, 1)
        subj, plain_body = mock_send.await_args.args[:2]
        self.assertIn("2 Credential(s) Failed Authentication", subj)
        self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, plain_body)
        self.assertIn("FZ58776", plain_body)
        self.assertIn("FZ48062", plain_body)


if __name__ == "__main__":
    unittest.main()
