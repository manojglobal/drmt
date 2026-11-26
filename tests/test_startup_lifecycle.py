"""Unit tests for startup lifecycle, settings parsing, and auto-reloader watcher."""

import os
import tempfile
import shutil
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
import zoneinfo

from aiohttp import web

import main
from database.db import get_db
from position.blocklist import clear_blocklist, get_blocked_user, is_blocked
from settings import Settings
from tests.helpers import isolated_test_db, suppress_email_notifications
from utils.reloader import FileWatcher, is_reloader_active

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")


class TestStartupLifecycleAndReloader(unittest.IsolatedAsyncioTestCase):
    """Tests main.py startup/quarantine/auth flows, Settings, and FileWatcher."""

    async def asyncSetUp(self):
        clear_blocklist()

    async def asyncTearDown(self):
        clear_blocklist()

    async def test_seed_admin_user_idempotent(self):
        """seed_admin_user creates default admin once and avoids duplicates."""
        async with isolated_test_db():
            await main.seed_admin_user()
            await main.seed_admin_user()
            async with get_db() as db:
                cur = await db.execute("SELECT username FROM users")
                rows = await cur.fetchall()
            self.assertEqual([r[0] for r in rows], ["admin"])

    async def test_on_startup_passive_quarantine_handles_missing_and_expired_tokens(self):
        """on_startup quarantines missing & expired tokens without NameError or broker login calls."""
        yesterday = (datetime.now(tz=_IST) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now(tz=_IST).strftime("%Y-%m-%d %H:%M:%S")

        fake_creds = {
            "AB_MISSING": {"broker": "ALICE BLUE", "client_id": "AB_MISSING", "token": "", "token_created_at": ""},
            "MS_EXPIRED": {"broker": "MSTOCK", "client_id": "MS_EXPIRED", "token": "old_tok", "token_created_at": yesterday},
            "FT_VALID": {"broker": "FLATTRADE", "client_id": "FT_VALID", "token": "ok_tok", "token_created_at": today},
        }

        async def _noop_coro(*args, **kwargs):
            return None

        async with isolated_test_db(), suppress_email_notifications():
            app = web.Application()
            with (
                patch("credentials.scheduler.is_before_daily_refresh", return_value=False),
                patch.object(main.core_engine, "start", new=AsyncMock()),
                patch("main.init_db", new=AsyncMock()),
                patch("credentials.dynamic_loader.fetch_and_sync_credentials", new=AsyncMock(return_value=3)),
                patch("main.init_email_session", new=AsyncMock()),
                patch("main.get_configured_client_ids", return_value=set(fake_creds.keys())),
                patch("credentials.dynamic_loader.get_dynamic_credentials", side_effect=lambda cid: fake_creds.get(cid)),
                patch("main.get_client_username", side_effect=lambda cid: f"Name_{cid}"),
                patch.object(main.health_monitor, "start", new=AsyncMock()),
                patch("main.start_log_cleanup_loop", side_effect=_noop_coro),
                patch("credentials.scheduler.start_daily_credential_scheduler", side_effect=_noop_coro),
                patch("credentials.watcher.start_blocked_credentials_watcher", side_effect=_noop_coro),
                patch("credentials.scheduler.start_axis_token_refresh_worker", side_effect=_noop_coro),
            ):
                await main.on_startup(app)
                for key in ("daily_scheduler_task", "blocked_watcher_task", "axis_refresh_task"):
                    if key in app and not app[key].done():
                        app[key].cancel()

            self.assertTrue(is_blocked("AB_MISSING"))
            self.assertIn("Missing access token", get_blocked_user("AB_MISSING")["reason"])
            self.assertTrue(is_blocked("MS_EXPIRED"))
            self.assertIn("Session token expired", get_blocked_user("MS_EXPIRED")["reason"])
            self.assertFalse(is_blocked("FT_VALID"))

    async def _run_startup_scenario(self, fake_creds: dict, before_refresh: bool):
        """Run main.on_startup with mocked services and return (mock_send_email, mock_auth_warn)."""
        async def _noop_coro(*args, **kwargs):
            return None

        mock_send = AsyncMock()
        mock_auth_warn = AsyncMock()
        async with isolated_test_db():
            app = web.Application()
            with ExitStack() as stack:
                for p in (
                    patch("credentials.scheduler.is_before_daily_refresh", return_value=before_refresh),
                    patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"),
                    patch("utils.email.send_error_email", mock_send),
                    patch.object(main.logger_auth, "warn", mock_auth_warn),
                    patch.object(main.core_engine, "start", new=AsyncMock()),
                    patch("main.init_db", new=AsyncMock()),
                    patch("credentials.dynamic_loader.fetch_and_sync_credentials", new=AsyncMock(return_value=len(fake_creds))),
                    patch("main.init_email_session", new=AsyncMock()),
                    patch("main.get_configured_client_ids", return_value=set(fake_creds.keys())),
                    patch("utils.user_mapping.get_configured_client_ids", return_value=set(fake_creds.keys())),
                    patch("credentials.dynamic_loader.get_dynamic_credentials", side_effect=lambda cid: fake_creds.get(cid)),
                    patch("credentials.dynamic_loader.get_client_email", side_effect=lambda cid: f"{cid.lower()}@gmail.com"),
                    patch("main.get_client_username", side_effect=lambda cid: f"Name_{cid}"),
                    patch("utils.user_mapping.get_client_username", side_effect=lambda cid: f"Name_{cid}"),
                    patch.object(main.health_monitor, "start", new=AsyncMock()),
                    patch("main.start_log_cleanup_loop", side_effect=_noop_coro),
                    patch("credentials.scheduler.start_daily_credential_scheduler", side_effect=_noop_coro),
                    patch("credentials.watcher.start_blocked_credentials_watcher", side_effect=_noop_coro),
                    patch("credentials.scheduler.start_axis_token_refresh_worker", side_effect=_noop_coro),
                ):
                    stack.enter_context(p)
                await main.on_startup(app)
                for key in ("daily_scheduler_task", "blocked_watcher_task", "axis_refresh_task"):
                    if key in app and not app[key].done():
                        app[key].cancel()
        return mock_send, mock_auth_warn

    async def test_on_startup_before_0910_defers_credential_errors_and_emails(self):
        """Before 09:10 IST daily refresh, on_startup performs zero quarantine, zero auth warnings, and zero emails."""
        yesterday = (datetime.now(tz=_IST) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now(tz=_IST).strftime("%Y-%m-%d %H:%M:%S")
        fake_creds = {
            "FZ58776": {"broker": "FLATTRADE", "client_id": "FZ58776", "token": "ok1", "token_created_at": today, "api_key": "", "api_secret": ""},
            "FZ48062": {"broker": "FLATTRADE", "client_id": "FZ48062", "token": "", "token_created_at": "", "api_key": "", "api_secret": ""},
            "MS_EXPIRED": {"broker": "MSTOCK", "client_id": "MS_EXPIRED", "token": "old_tok", "token_created_at": yesterday},
        }

        mock_send, mock_auth_warn = await self._run_startup_scenario(fake_creds, before_refresh=True)

        self.assertFalse(is_blocked("FZ58776"))
        self.assertFalse(is_blocked("FZ48062"))
        self.assertFalse(is_blocked("MS_EXPIRED"))
        mock_auth_warn.assert_not_awaited()
        mock_send.assert_not_awaited()

    async def test_on_startup_after_0910_quarantines_missing_flattrade_keys_without_separate_email(self):
        """After 09:10 IST, on_startup quarantines missing Flattrade SDK keys without sending a separate startup email."""
        from credentials.flattrade import FLATTRADE_MISSING_KEYS_MESSAGE, reset_missing_keys_alert_cache

        reset_missing_keys_alert_cache()
        yesterday = (datetime.now(tz=_IST) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        fake_creds = {
            "FZ58776": {"broker": "FLATTRADE", "client_id": "FZ58776", "token": "", "token_created_at": "", "api_key": "", "api_secret": ""},
            "FZ48062": {"broker": "FLATTRADE", "client_id": "FZ48062", "token": "old2", "token_created_at": yesterday, "api_key": "", "api_secret": ""},
        }

        mock_send, _ = await self._run_startup_scenario(fake_creds, before_refresh=False)

        self.assertTrue(is_blocked("FZ58776"))
        self.assertTrue(is_blocked("FZ48062"))
        self.assertEqual(get_blocked_user("FZ58776")["reason"], FLATTRADE_MISSING_KEYS_MESSAGE)
        self.assertEqual(get_blocked_user("FZ48062")["reason"], FLATTRADE_MISSING_KEYS_MESSAGE)
        mock_send.assert_not_awaited()

    async def test_authenticate_configured_credentials_aggregates_results(self):
        """authenticate_configured_credentials reports succeeded and failed counts accurately."""
        async with isolated_test_db(), suppress_email_notifications():
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token)
                    VALUES ('FLATTRADE', 'C_OK', 'k1', 's1', 'FLATTRADE:C_OK', 'working', ''),
                           ('MSTOCK', 'C_FAIL', 'k2', 's2', 'MSTOCK:C_FAIL', 'working', '')
                    """
                )
                await db.commit()

            async def _fake_refresh(cred, send_email=True):
                if cred.client_id == "C_OK":
                    return True
                raise RuntimeError("Bad password")

            with (
                patch("main.get_configured_client_ids", return_value={"C_OK", "C_FAIL"}),
                patch("position.chronos.refresh_token_with_backoff", side_effect=_fake_refresh),
                patch("credentials.dynamic_loader.get_dynamic_credentials", return_value={}),
                patch("credentials.dynamic_loader.get_client_email", side_effect=lambda cid: f"{cid.lower()}@example.com"),
                patch("main.get_client_username", side_effect=lambda cid: f"User_{cid}"),
            ):
                succeeded, failed, results = await main.authenticate_configured_credentials()

            self.assertEqual(succeeded, ["FLATTRADE:C_OK"])
            self.assertEqual(failed, [("MSTOCK:C_FAIL", "Bad password")])
            self.assertEqual(len(results), 2)

    def test_settings_dropbox_and_flow_flags(self):
        """Settings parses USE_DROPBOX_CREDENTIALS toggle and Dropbox OAuth variables from env file."""
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tf:
            tf.write(
                "USE_DROPBOX_CREDENTIALS=true\n"
                "DRMT_DROPBOX_APP_KEY=app_k\n"
                "DRMT_DROPBOX_APP_SECRET=app_s\n"
                "DRMT_DROPBOX_REFRESH_TOKEN=ref_t\n"
                "DRMT_DROPBOX_FILE_PATH=/custom.csv\n"
                "FLATTRADE_FLOW=SDK\n"
                "ALICE_BLUE_FLOW=WEB\n"
            )
            env_path = tf.name

        try:
            s = Settings(_env_file=env_path)
            self.assertTrue(s.USE_DROPBOX_CREDENTIALS)
            self.assertEqual(s.DRMT_DROPBOX_APP_KEY, "app_k")
            self.assertEqual(s.DRMT_DROPBOX_APP_SECRET, "app_s")
            self.assertEqual(s.DRMT_DROPBOX_REFRESH_TOKEN, "ref_t")
            self.assertEqual(s.DRMT_DROPBOX_FILE_PATH, "/custom.csv")
            self.assertEqual(s.FLATTRADE_FLOW, "SDK")
            self.assertEqual(s.ALICE_BLUE_FLOW, "WEB")
        finally:
            os.remove(env_path)

    def test_file_watcher_and_reloader_guards(self):
        """FileWatcher detects .py and .env changes while ignoring data/logs/dist directories."""
        tmp_dir = tempfile.mkdtemp(prefix="drmt_watch_")
        try:
            os.makedirs(os.path.join(tmp_dir, "data", "logs"), exist_ok=True)
            os.makedirs(os.path.join(tmp_dir, "dist"), exist_ok=True)
            py_file = os.path.join(tmp_dir, "sample.py")
            ignored_file = os.path.join(tmp_dir, "data", "ignored.py")
            with open(py_file, "w") as f:
                f.write("x = 1\n")
            with open(ignored_file, "w") as f:
                f.write("y = 1\n")

            watcher = FileWatcher(tmp_dir)
            self.assertIsNone(watcher.check_for_changes())

            # Modify ignored file -> should NOT trigger reload
            os.utime(ignored_file, (watcher.mtimes.get(ignored_file, 0) + 10, watcher.mtimes.get(ignored_file, 0) + 10))
            self.assertIsNone(watcher.check_for_changes())

            # Modify .py file -> MUST trigger reload
            new_mtime = watcher.mtimes[py_file] + 5.0
            os.utime(py_file, (new_mtime, new_mtime))
            self.assertEqual(watcher.check_for_changes(), "sample.py")

            # Child marker disables supervisor recursion
            with patch.dict(os.environ, {"_DRMT_RELOADER_CHILD": "1"}):
                self.assertFalse(is_reloader_active())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
