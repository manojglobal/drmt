"""Unit tests for logging sanitization, rotation/retention, email alerts, and embedded SPA assets."""

import asyncio
import os
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import utils.email as email_mod
from logger.logger import (
    ServiceLogger,
    clean_error_message,
    cleanup_old_logs,
    sanitize_log_message,
)
from tests.helpers import isolated_log_dir
from web.embedded import get_embedded_asset, setup_embedded_frontend


class TestLoggingAndEmailUtilities(unittest.IsolatedAsyncioTestCase):
    """Tests log sanitization, HTML error stripping, 30-day rotation cleanup, and email alerts."""

    def test_sanitize_log_message_masks_secrets_and_keeps_instrument_tokens(self):
        """Sensitive secrets are masked while numeric exchange instrument tokens remain visible."""
        raw = (
            'password="MySecret!1" totp_secret=JBSWY3DPEHPK3PXP api_key="key123" '
            'api_secret="sec456" DRMT_DROPBOX_APP_SECRET=dbx_sec DRMT_DROPBOX_REFRESH_TOKEN=dbx_ref '
            'Bearer abcdef1234567890wxyz jKey=suser_tok_99 "token": "3787"'
        )
        sanitized = sanitize_log_message(raw)
        for secret in ("MySecret!1", "JBSWY3DPEHPK3PXP", "key123", "sec456", "dbx_sec", "dbx_ref", "suser_tok_99"):
            self.assertNotIn(secret, sanitized)
        self.assertIn("abcdef***wxyz", sanitized)
        self.assertIn('"token": "3787"', sanitized)

    def test_clean_error_message_strips_html_and_proxy_urls(self):
        """HTML 502 error pages and internal proxy URLs collapse to clean single-line messages."""
        html_err = "502, message='Bad Gateway', url=URL('https://proxy.example.com/proxy')\n<html><body><h1>502 Bad Gateway</h1></body></html>"
        cleaned = clean_error_message(html_err)
        self.assertNotIn("<html>", cleaned)
        self.assertNotIn("proxy.example.com", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertIn("HTTP 502 (Bad Gateway)", cleaned)

    async def test_service_logger_ring_buffer_and_30day_cleanup(self):
        """ServiceLogger keeps newest-first max 100 entries and cleanup_old_logs purges files >30 days."""
        async with isolated_log_dir() as tmp_log_dir:
            svc = ServiceLogger("test_svc")
            for i in range(105):
                await svc.info(f"msg-{i}")
            history = svc.get_history()
            self.assertEqual(len(history), 100)
            self.assertEqual(history[0].message, "msg-104")

            # Allow pending file writes to finish, then create an expired log file (45 days old)
            await asyncio.sleep(0.05)
            svc_dir = os.path.join(tmp_log_dir, "test_svc")
            os.makedirs(svc_dir, exist_ok=True)
            old_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
            old_path = os.path.join(svc_dir, f"app_{old_date}.log")
            with open(old_path, "w") as f:
                f.write("stale log line\n")
            old_ts = time.time() - (45 * 86400)
            os.utime(old_path, (old_ts, old_ts))

            deleted = await cleanup_old_logs(retention_days=30)
            self.assertGreaterEqual(deleted, 1)
            self.assertFalse(os.path.exists(old_path))

    async def test_email_formatting_dedup_and_missing_smtp_graceful(self):
        """Email module formats HTML tables cleanly, dedups repeated subjects within 30s, and skips missing SMTP."""
        email_mod._RECENT_SUBJECTS.clear()
        email_mod._EMAIL_SEMAPHORE = asyncio.Semaphore(1)

        plain, html_out = email_mod.format_drmt_email("Alert", "Line 1\nLine 2")
        self.assertIn("Hello,", plain)
        self.assertIn("Team Quantivo", html_out)

        sent_calls = []

        def _fake_sync(subject, plain_body, html_body, *args):
            sent_calls.append(subject)

        with (
            patch("utils.email.settings.EMAIL_USER", "sender@example.com"),
            patch("utils.email.settings.EMAIL_PASS", "smtp_pass"),
            patch("utils.email.settings.EMAIL_TO", "ops@example.com"),
            patch("utils.email._send_email_sync", side_effect=_fake_sync),
        ):
            await email_mod.send_error_email("Unique Failure Alert", "Details")
            await email_mod.send_error_email("Unique Failure Alert", "Details")  # Suppressed by 30s dedup
            self.assertEqual(len(sent_calls), 1)

        # Missing SMTP pass does not raise
        email_mod._RECENT_SUBJECTS.clear()
        with patch("utils.email.settings.EMAIL_USER", ""), patch("utils.email.settings.EMAIL_PASS", ""):
            await email_mod.send_error_email("Should Skip Gracefully", "Body")

    async def test_flattrade_missing_keys_single_merged_email_and_sdk_toggle(self):
        """Merged Flattrade missing API key/secret alert sends one email for all users only in SDK mode."""
        from credentials.flattrade import (
            FLATTRADE_MISSING_KEYS_MESSAGE,
            notify_flattrade_missing_keys,
            reset_missing_keys_alert_cache,
        )
        import utils.email as email_mod

        reset_missing_keys_alert_cache()
        email_mod._RECENT_SUBJECTS.clear()
        dispatched = []

        def _capture_sync(subject, plain_body, html_body, *args):
            dispatched.append((subject, plain_body, html_body))

        fake_creds = {
            "FZ58776": {"broker": "FLATTRADE", "client_id": "FZ58776", "api_key": "", "api_secret": ""},
            "FZ48062": {"broker": "FLATTRADE", "client_id": "FZ48062", "api_key": "k_only", "api_secret": ""},
            "FZ59716": {"broker": "FLATTRADE", "client_id": "FZ59716", "api_key": "ak", "api_secret": "as"},
        }

        with (
            patch("utils.email.settings.EMAIL_USER", "sender@example.com"),
            patch("utils.email.settings.EMAIL_PASS", "smtp_pass"),
            patch("utils.email.settings.EMAIL_TO", "ops@example.com"),
            patch("utils.email._send_email_sync", side_effect=_capture_sync),
            patch("utils.user_mapping.get_configured_client_ids", return_value=set(fake_creds.keys())),
            patch("utils.user_mapping.get_client_username", side_effect=lambda cid: f"User_{cid}"),
            patch("credentials.dynamic_loader.get_dynamic_credentials", side_effect=lambda cid: fake_creds.get(cid)),
            patch("credentials.dynamic_loader.get_client_email", side_effect=lambda cid: f"{cid.lower()}@gmail.com"),
        ):
            # WEB flow should skip missing SDK key alerts
            with patch("credentials.flattrade.settings.FLATTRADE_FLOW", "WEB"):
                res_web = await notify_flattrade_missing_keys(source="UnitTest")
            self.assertEqual(res_web, [])
            self.assertEqual(len(dispatched), 0)

            # SDK flow sends exactly ONE merged email containing both missing-key users
            with patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"):
                res_sdk = await notify_flattrade_missing_keys(source="UnitTest")
                # Duplicate signature without force does NOT resend
                await notify_flattrade_missing_keys(source="UnitTest")

            self.assertEqual(len(res_sdk), 2)
            self.assertEqual(len(dispatched), 1)
            subj, plain_body, html_body = dispatched[0]
            self.assertIn("2 Accounts", subj)
            self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, plain_body)
            self.assertIn("FZ58776", plain_body)
            self.assertIn("FZ48062", plain_body)
            self.assertNotIn("FZ59716", plain_body)
            self.assertIn("API Key, Secret Key", html_body)
            self.assertIn("Secret Key", html_body)


class TestEmbeddedFrontendAssets(AioHTTPTestCase):
    """Tests web/embedded.py asset serving for /, /app.jsx, and /style.css."""

    async def get_application(self):
        app = web.Application()
        setup_embedded_frontend(app)
        return app

    async def test_embedded_routes_serve_frontend_assets(self):
        """SPA index, app.jsx, and style.css return 200 OK with expected frontend symbols."""
        r_idx = await self.client.get("/")
        self.assertEqual(r_idx.status, 200)
        self.assertIn("text/html", r_idx.headers.get("Content-Type", ""))
        self.assertIn("Disaster Recovery Management Tool", await r_idx.text())

        r_jsx = await self.client.get("/app.jsx")
        self.assertEqual(r_jsx.status, 200)
        jsx_body = await r_jsx.text()
        self.assertIn("/ws/positions", jsx_body)
        self.assertIn("/api/exit", jsx_body)
        self.assertIn("isIntradayProduct", jsx_body)
        self.assertIn("isHoldingProduct", jsx_body)
        self.assertIn("getPositionProduct", jsx_body)
        self.assertIn("useState('INTRADAY')", jsx_body)

        r_css = await self.client.get("/style.css")
        self.assertEqual(r_css.status, 200)
        self.assertTrue(len(get_embedded_asset("style.css")) > 100)


if __name__ == "__main__":
    unittest.main()
