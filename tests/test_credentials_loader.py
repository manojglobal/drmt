"""Unit tests for credentials module (crypto, dedup, dynamic_loader)."""

import unittest
from unittest.mock import patch
import tempfile
import os

from credentials.crypto import (
    hash_password,
    verify_password,
    generate_session_token,
    sha256_hex,
    totp_now,
)
from credentials.dynamic_loader import (
    compute_credential_signature,
    get_dynamic_credentials,
    _normalize_dropbox_url,
    fetch_and_sync_credentials,
)
from credentials.dedup import save_working_credential
from models.schemas import Credential
from tests.helpers import isolated_test_db
from database.db import get_db
from tests.fixtures.mock_credentials import MOCK_CSV_CONTENT


class TestCredentialsLoader(unittest.IsolatedAsyncioTestCase):
    """Test suite for credential encryption, deduplication, and dynamic loading."""

    def test_crypto_hashing_and_verification(self):
        """Verify bcrypt hashing and password verification."""
        password = "SecureTradingPass123!"
        hashed = hash_password(password)
        self.assertTrue(verify_password(password, hashed))
        self.assertFalse(verify_password("WrongPassword", hashed))

    def test_session_token_and_sha256(self):
        """Verify token generator and sha256 helper."""
        token = generate_session_token()
        self.assertEqual(len(token), 64)  # 32 bytes hex = 64 chars

        digest = sha256_hex("test_input")
        self.assertEqual(digest, "952822de6a627ea459e1e7a8964191c79fccfb14ea545d93741b5cf3ed71a09a")

    def test_totp_generation(self):
        """Verify TOTP generation with valid base32 secret."""
        secret = "2364AP5Z35V2R4A3AHDJ4K464357M2K6"
        otp = totp_now(secret)
        self.assertTrue(otp.isdigit())
        self.assertEqual(len(otp), 6)
        self.assertEqual(totp_now(""), "")

    def test_compute_credential_signature(self):
        """Verify SHA-256 signature computation detects changes in sensitive fields."""
        cred1 = {
            "broker": "FLATTRADE",
            "client_id": "FZ48062",
            "password": "Pass1",
            "totp_secret": "TOTP1",
        }
        cred2 = dict(cred1)
        self.assertEqual(compute_credential_signature(cred1), compute_credential_signature(cred2))

        # Change password
        cred2["password"] = "Pass2"
        self.assertNotEqual(compute_credential_signature(cred1), compute_credential_signature(cred2))

        # None/empty handling
        self.assertEqual(compute_credential_signature(None), "")

    def test_normalize_dropbox_url(self):
        """Verify Dropbox URL conversion from dl=0 to dl=1 for direct raw file downloads."""
        url_dl0 = "https://www.dropbox.com/s/12345/creds.csv?dl=0"
        self.assertEqual(_normalize_dropbox_url(url_dl0), "https://www.dropbox.com/s/12345/creds.csv?dl=1")

        url_no_param = "https://www.dropbox.com/s/12345/creds.csv"
        self.assertEqual(_normalize_dropbox_url(url_no_param), "https://www.dropbox.com/s/12345/creds.csv?dl=1")

        self.assertEqual(_normalize_dropbox_url(""), "")

    async def test_dedup_save_working_credential(self):
        """Verify save_working_credential marks other credentials with same broker/client_id as skipped."""
        async with isolated_test_db():
            async with get_db() as conn:
                # Insert initial active credential
                await conn.execute(
                    """
                    INSERT INTO credentials 
                    (broker, client_id, api_key, api_secret, static_ip, fingerprint, status, token)
                    VALUES (?, ?, ?, ?, ?, ?, 'working', ?)
                    """,
                    ("FLATTRADE", "FZ48062", "", "", "172.31.8.142", "FLATTRADE:FZ48062:old", "old_token"),
                )
                await conn.commit()

            # Save a new working credential
            new_cred = Credential(
                broker="FLATTRADE",
                client_id="FZ48062",
                static_ip="172.31.8.142",
                fingerprint="FLATTRADE:FZ48062",
                status="working",
                token="new_working_token",
            )
            await save_working_credential(new_cred)

            async with get_db() as conn:
                cursor = await conn.execute(
                    "SELECT fingerprint, status, token FROM credentials WHERE client_id = ?",
                    ("FZ48062",),
                )
                rows = await cursor.fetchall()
                # The old record should be marked 'skipped' and the new one 'working'
                status_by_fp = {r[0]: (r[1], r[2]) for r in rows}
                self.assertEqual(status_by_fp["FLATTRADE:FZ48062:old"][0], "skipped")
                self.assertEqual(status_by_fp["FLATTRADE:FZ48062"][0], "working")
                self.assertEqual(status_by_fp["FLATTRADE:FZ48062"][1], "new_working_token")

    async def test_fetch_and_sync_credentials_from_mock_csv(self):
        """Verify CSV syncing updates in-memory mapping and populates database."""
        async with isolated_test_db():
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf:
                tf.write(MOCK_CSV_CONTENT)
                temp_csv_path = tf.name

            try:
                with patch("settings.settings.CREDENTIALS_CSV_PATH", temp_csv_path):
                    with patch("settings.settings.USE_DROPBOX_CREDENTIALS", False):
                        count = await fetch_and_sync_credentials()
                        self.assertEqual(count, 2)

                        # Check in-memory retrieval
                        d_fz = get_dynamic_credentials("FZ48062")
                        self.assertIsNotNone(d_fz)
                        self.assertEqual(d_fz["client_id"], "FZ48062")
                        self.assertEqual(d_fz["broker"], "FLATTRADE")
                        self.assertEqual(d_fz["token"], "mock_token_fz")

                        d_ab = get_dynamic_credentials("AB12345")
                        self.assertIsNotNone(d_ab)
                        self.assertEqual(d_ab["client_id"], "AB12345")
                        self.assertEqual(d_ab["broker"], "ALICEBLUE")
            finally:
                if os.path.exists(temp_csv_path):
                    os.remove(temp_csv_path)

    async def test_dropbox_oauth_readonly_and_no_local_csv_overwrite(self):
        """Verify USE_DROPBOX_CREDENTIALS=True uses read-only OAuth download and never overwrites static CSV."""
        import credentials.dynamic_loader as dl

        called_urls = []

        class _FakeResponse:
            def __init__(self, status=200, text_body="", json_body=None, content_type="application/octet-stream"):
                self.status = status
                self._text = text_body
                self._json = json_body
                self.headers = {"Content-Type": content_type}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self, encoding="utf-8", errors="replace"):
                return self._text

            async def json(self, content_type=None):
                return self._json

        class _FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def post(self, url, **kwargs):
                called_urls.append(url)
                if "oauth2/token" in url:
                    return _FakeResponse(200, json_body={"access_token": "short_lived_tok", "expires_in": 14400})
                if "files/download" in url:
                    return _FakeResponse(200, text_body=MOCK_CSV_CONTENT)
                raise AssertionError(f"Unexpected non-read Dropbox endpoint: {url}")

        async with isolated_test_db():
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf:
                sentinel_content = "SENTINEL_STATIC_CSV_UNTOUCHED"
                tf.write(sentinel_content)
                temp_csv_path = tf.name

            try:
                dl._dropbox_access_token = ""
                dl._dropbox_token_expires_at = 0.0
                dl._dropbox_token_cache_key = ""
                with patch("settings.settings.USE_DROPBOX_CREDENTIALS", True), \
                     patch("settings.settings.DRMT_DROPBOX_APP_KEY", "test_app_key"), \
                     patch("settings.settings.DRMT_DROPBOX_APP_SECRET", "test_app_secret"), \
                     patch("settings.settings.DRMT_DROPBOX_REFRESH_TOKEN", "test_refresh_token"), \
                     patch("settings.settings.DRMT_DROPBOX_FILE_PATH", "/plato_client_credentials.csv"), \
                     patch("settings.settings.CREDENTIALS_CSV_PATH", temp_csv_path), \
                     patch("credentials.dynamic_loader.aiohttp.ClientSession", _FakeSession):
                    count = await fetch_and_sync_credentials()
                    self.assertEqual(count, 2)
                    self.assertEqual(
                        called_urls,
                        [
                            "https://api.dropboxapi.com/oauth2/token",
                            "https://content.dropboxapi.com/2/files/download",
                        ],
                    )
                    # Local static CSV must never be overwritten
                    with open(temp_csv_path, "r", encoding="utf-8") as f:
                        self.assertEqual(f.read(), sentinel_content)
            finally:
                if os.path.exists(temp_csv_path):
                    os.remove(temp_csv_path)

    async def test_dropbox_html_rejected_falls_back_to_local_csv(self):
        """Verify HTML responses from Dropbox are rejected and fall back to reading local static CSV."""
        class _FakeHtmlResponse:
            def __init__(self):
                self.status = 200
                self.headers = {"Content-Type": "text/html; charset=utf-8"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def text(self, encoding="utf-8", errors="replace"):
                return "<!DOCTYPE html><html><title>Dropbox - File Deleted</title></html>"

        class _FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, **kwargs):
                return _FakeHtmlResponse()

        async with isolated_test_db():
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf:
                tf.write(MOCK_CSV_CONTENT)
                temp_csv_path = tf.name

            try:
                with patch("settings.settings.USE_DROPBOX_CREDENTIALS", True), \
                     patch("settings.settings.DRMT_DROPBOX_APP_KEY", ""), \
                     patch("settings.settings.DRMT_DROPBOX_APP_SECRET", ""), \
                     patch("settings.settings.DRMT_DROPBOX_REFRESH_TOKEN", ""), \
                     patch("settings.settings.DROPBOX_CREDENTIALS_URL", "https://www.dropbox.com/s/fake/creds.csv?dl=1"), \
                     patch("settings.settings.CREDENTIALS_CSV_PATH", temp_csv_path), \
                     patch("credentials.dynamic_loader.aiohttp.ClientSession", _FakeSession):
                    count = await fetch_and_sync_credentials()
                    self.assertEqual(count, 2)
                    with open(temp_csv_path, "r", encoding="utf-8") as f:
                        self.assertEqual(f.read(), MOCK_CSV_CONTENT)
            finally:
                if os.path.exists(temp_csv_path):
                    os.remove(temp_csv_path)

    async def test_use_dropbox_false_skips_dropbox_and_archives_missing_clients(self):
        """USE_DROPBOX_CREDENTIALS=False makes zero Dropbox calls, archives removed accounts, and reports source info."""
        import credentials.dynamic_loader as dl

        async with isolated_test_db():
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token)
                    VALUES ('MSTOCK', 'OLD_REMOVED', 'k', 's', 'MSTOCK:OLD_REMOVED', 'working', 'old_tok')
                    """
                )
                await db.commit()

            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf:
                tf.write(MOCK_CSV_CONTENT)
                temp_csv_path = tf.name

            try:
                with (
                    patch("settings.settings.USE_DROPBOX_CREDENTIALS", False),
                    patch("settings.settings.CREDENTIALS_CSV_PATH", temp_csv_path),
                    patch("credentials.dynamic_loader._fetch_csv_from_dropbox_api", side_effect=AssertionError("Must not call Dropbox when False")),
                ):
                    count = await fetch_and_sync_credentials()
                    self.assertEqual(count, 2)

                    info = dl.get_credential_source_info()
                    self.assertFalse(info["use_dropbox"])
                    self.assertEqual(info["active_source"], "LOCAL_CSV")
                    self.assertEqual(info["configured_accounts"], 2)

                async with get_db() as db:
                    cur = await db.execute("SELECT status FROM credentials WHERE client_id = 'OLD_REMOVED'")
                    row = await cur.fetchone()
                    self.assertEqual(row[0], "archived")
            finally:
                if os.path.exists(temp_csv_path):
                    os.remove(temp_csv_path)

    async def test_fallback_load_from_db_preserves_token_updated_at(self):
        """_fallback_load_from_db loads working accounts and token_updated_at into _client_creds."""
        import credentials.dynamic_loader as dl

        async with isolated_test_db():
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token, token_updated_at)
                    VALUES ('FLATTRADE', 'FZ59716', 'ak', 'as', 'FLATTRADE:FZ59716', 'working', 'tok_db', '2026-11-18 09:10:00')
                    """
                )
                await db.commit()

            dl._configured_cids.clear()
            dl._client_creds.clear()
            await dl._fallback_load_from_db()
            cred = get_dynamic_credentials("FZ59716")
            self.assertIsNotNone(cred)
            self.assertEqual(cred["token"], "tok_db")
            self.assertEqual(cred["token_created_at"], "2026-11-18 09:10:00")

    async def test_dynamic_sync_preserves_runtime_sid_base_url_sub_account_and_kotak_fields(self):
        """CSV reloads omitting runtime session fields must not wipe existing SQLite or memory sid/base_url/sub_account_id."""
        import credentials.dynamic_loader as dl

        kotak_csv = (
            "id,broker,credentials,user_id,first_name,last_name,static_ip,client_id,access_token,access_token_created_at\n"
            '1,KOTAK,"{""api_key"": ""k_ak"", ""totp_secret"": ""JBSWY3DPEHPK3PXP""}",10,Raj,Kumar,1.2.3.4,VFIU2,,\n'
        )
        async with isolated_test_db():
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO credentials
                    (broker, client_id, api_key, api_secret, fingerprint, status, token, sid, base_url, sub_account_id, mobile_number, mpin)
                    VALUES ('KOTAK', 'VFIU2', 'k_ak', '', 'KOTAK:VFIU2', 'working', 'live_kotak_tok', 'live_sid_99', 'https://e22.kotaksecurities.com', 'sub_axis_77', '9876543210', '1234')
                    """
                )
                await db.commit()

            dl._configured_cids.clear()
            dl._client_creds.clear()
            await dl._fallback_load_from_db()
            dl.update_dynamic_credential_token(
                "VFIU2",
                "live_kotak_tok",
                sid="live_sid_99",
                base_url="https://e22.kotaksecurities.com",
                sub_account_id="sub_axis_77",
            )

            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tf:
                tf.write(kotak_csv)
                temp_csv_path = tf.name

            try:
                with (
                    patch("settings.settings.USE_DROPBOX_CREDENTIALS", False),
                    patch("settings.settings.CREDENTIALS_CSV_PATH", temp_csv_path),
                ):
                    count = await fetch_and_sync_credentials()
                    self.assertEqual(count, 1)

                async with get_db() as db:
                    cur = await db.execute(
                        "SELECT token, sid, base_url, sub_account_id, mobile_number, mpin FROM credentials WHERE fingerprint = 'KOTAK:VFIU2'"
                    )
                    row = await cur.fetchone()
                    self.assertEqual(row[0], "live_kotak_tok")
                    self.assertEqual(row[1], "live_sid_99")
                    self.assertEqual(row[2], "https://e22.kotaksecurities.com")
                    self.assertEqual(row[3], "sub_axis_77")
                    self.assertEqual(row[4], "9876543210")
                    self.assertEqual(row[5], "1234")

                dyn = get_dynamic_credentials("VFIU2")
                self.assertEqual(dyn["sid"], "live_sid_99")
                self.assertEqual(dyn["base_url"], "https://e22.kotaksecurities.com")
                self.assertEqual(dyn["sub_account_id"], "sub_axis_77")
            finally:
                if os.path.exists(temp_csv_path):
                    os.remove(temp_csv_path)


if __name__ == "__main__":
    unittest.main()
