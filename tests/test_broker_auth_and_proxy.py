"""Unit tests for RESTProxyTransport and all 6 broker login/auth workflows."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from Crypto.PublicKey import RSA

from credentials.aliceblue import login_aliceblue
from credentials.axisdirect import decrypt_data, encrypt_aes, encrypt_rsa, login_axisdirect
from credentials.flattrade import login_flattrade, login_flattrade_sdk, login_flattrade_web
from credentials.fyers import get_fyers_auth_header, login_fyers
from credentials.httpclient import proxy_request
from credentials.kotakneo import (
    generate_user_kotak_neo_access_token,
    get_kotak_base_url,
    get_kotak_headers,
    login_kotakneo,
)
from credentials.mstock import login_mstock
from core.engine import core_engine
from models.schemas import Credential
from tests.helpers import isolated_test_db, suppress_email_notifications


class _FakeResponse:
    def __init__(self, payload, status=200, text_body=None):
        self._payload = payload
        self.status = status
        self._text = text_body if text_body is not None else (
            json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
        )

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self):
        return self._text

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SequencedSession:
    """Fake aiohttp session returning queued responses for GET/POST calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)


class TestBrokerAuthAndProxy(unittest.IsolatedAsyncioTestCase):
    """Tests proxy transport behavior and login/error handling for all 6 brokers."""

    async def asyncTearDown(self):
        core_engine.http_session = None

    async def test_proxy_request_retries_on_500_and_unpacks_wrapped_json(self):
        """proxy_request strips X-Static-IP, retries once without source_ip on 500, and parses JSON."""
        sess = _SequencedSession([
            _FakeResponse({"error": "proxy error"}, status=500),
            _FakeResponse({"status_code": 200, "json": '{"stat": "Ok", "token": "abc"}'}, status=200),
        ])
        core_engine.http_session = sess
        headers = {"X-Static-IP": "1.2.3.4", "Content-Type": "application/json"}

        async with suppress_email_notifications():
            result = await proxy_request("POST", "https://example.com/api", "172.31.8.133", headers=headers, json_data={"a": 1})

        self.assertEqual(result, {"stat": "Ok", "token": "abc"})
        self.assertEqual(len(sess.calls), 2)
        first_payload = sess.calls[0][2]["json"]
        retry_payload = sess.calls[1][2]["json"]
        self.assertEqual(first_payload["source_ip"], "172.31.8.133")
        self.assertNotIn("X-Static-IP", first_payload["headers"])
        self.assertNotIn("source_ip", retry_payload)

    async def test_proxy_request_raises_permission_error_on_401_and_runtime_on_400(self):
        """proxy_request converts wrapped 401/403 into PermissionError and 400 into RuntimeError."""
        async with suppress_email_notifications():
            core_engine.http_session = _SequencedSession([
                _FakeResponse({"status_code": 401, "json": {"status": "E", "statusCode": "MAIN01"}}, status=200)
            ])
            with self.assertRaises(PermissionError) as ctx_401:
                await proxy_request("POST", "https://emergeapi.flattrade.in/getPositonBook", "1.1.1.1")
            self.assertIn("MAIN01", str(ctx_401.exception))

            core_engine.http_session = _SequencedSession([
                _FakeResponse({"status_code": 400, "json": {"message": "Bad order"}}, status=200)
            ])
            with self.assertRaises(RuntimeError):
                await proxy_request("POST", "https://api.example.com/order", "1.1.1.1")

    async def test_flattrade_sdk_login_success_and_missing_keys_guard(self):
        """Flattrade SDK flow completes 3-step handshake and rejects missing api_key/api_secret."""
        async with isolated_test_db(), suppress_email_notifications():
            cred_no_keys = Credential(
                broker="FLATTRADE", client_id="FZ59716", password="pwd", totp_secret="JBSWY3DPEHPK3PXP",
                fingerprint="FLATTRADE:FZ59716", status="working"
            )
            with patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"):
                with self.assertRaises(ValueError) as ctx:
                    await login_flattrade(cred_no_keys)
                self.assertIn("missing 'api_key' and 'api_secret'", str(ctx.exception))

            cred_sdk = Credential(
                broker="FLATTRADE", client_id="FZ59716", password="pwd", totp_secret="JBSWY3DPEHPK3PXP",
                api_key="my_api_key", api_secret="my_api_secret", static_ip="172.31.8.133",
                fingerprint="FLATTRADE:FZ59716", status="working"
            )
            with (
                patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"),
                patch("credentials.flattrade.proxy_request", new=AsyncMock(side_effect=[
                    "sid_val_123",
                    {"RedirectURL": "https://localhost/?code=req_code_99"},
                    {"stat": "Ok", "token": "ft_sdk_token_xyz"},
                ])),
            ):
                tok = await login_flattrade_sdk(cred_sdk)
                self.assertEqual(tok, "ft_sdk_token_xyz")
                self.assertEqual(cred_sdk.token, "ft_sdk_token_xyz")

    async def test_flattrade_web_login_success_and_invalid_password(self):
        """Flattrade Web quickAuth extracts susertoken and surfaces login rejection error."""
        async with isolated_test_db(), suppress_email_notifications():
            cred = Credential(
                broker="FLATTRADE", client_id="FZ48062", password="pwd", totp_secret="JBSWY3DPEHPK3PXP",
                static_ip="172.31.8.142", fingerprint="FLATTRADE:FZ48062", status="working"
            )
            with patch("credentials.flattrade.proxy_request", new=AsyncMock(return_value={
                "quickauthresp": {"stat": "Ok", "susertoken": "web_suser_tok"}
            })):
                tok = await login_flattrade_web(cred)
                self.assertEqual(tok, "web_suser_tok")

            with patch("credentials.flattrade.proxy_request", new=AsyncMock(return_value={
                "quickauthresp": {"stat": "Not_Ok", "emsg": "Invalid Input : Wrong Password"}
            })):
                with self.assertRaises(ValueError) as err:
                    await login_flattrade_web(cred)
                self.assertIn("Wrong Password", str(err.exception))

    async def test_mstock_2step_login_success_and_failure(self):
        """MStock login performs login -> verifytotp -> jwtToken and fails clearly on missing token."""
        async with isolated_test_db(), suppress_email_notifications():
            cred = Credential(
                broker="MSTOCK", client_id="P645M2", password="pwd", totp_secret="JBSWY3DPEHPK3PXP",
                api_key="priv_k", static_ip="172.31.8.133", fingerprint="MSTOCK:P645M2", status="working"
            )
            with patch("credentials.mstock.proxy_request", new=AsyncMock(side_effect=[
                {"data": {"refreshToken": "ref_ms_1"}},
                {"data": {"jwtToken": "jwt_ms_final"}},
            ])):
                tok = await login_mstock(cred)
                self.assertEqual(tok, "jwt_ms_final")

            with patch("credentials.mstock.proxy_request", new=AsyncMock(return_value={"message": "Invalid credentials"})):
                with self.assertRaises(ValueError) as ctx:
                    await login_mstock(cred)
                self.assertIn("Invalid credentials", str(ctx.exception))

    async def test_aliceblue_6step_login(self):
        """AliceBlue 6-step AES-CBC + SHA256 checksum login yields userSession token."""
        async with isolated_test_db(), suppress_email_notifications():
            core_engine.http_session = _SequencedSession([
                _FakeResponse({"status": "Ok"}),
                _FakeResponse({"status": "Ok", "result": [{"encKey": "my_aes_enc_key"}]}),
                _FakeResponse({"status": "Ok", "result": [{"token": "temp_tok"}]}),
                _FakeResponse({"status": "Ok", "result": [{"accessToken": "acc_tok"}]}),
                _FakeResponse({"status": "Ok", "result": [{"redirectUrl": "https://cb/?authCode=AC_77"}]}),
                _FakeResponse({"stat": "Ok", "userSession": "alice_session_final"}),
            ])
            cred = Credential(
                broker="ALICE BLUE", client_id="1923416", password="pwd", totp_secret="JBSWY3DPEHPK3PXP",
                api_key="vendor_k", api_secret="app_sec", fingerprint="ALICE BLUE:1923416", status="working"
            )
            tok = await login_aliceblue(cred)
            self.assertEqual(tok, "alice_session_final")

    async def test_fyers_auth_header_and_profile_verification(self):
        """Fyers formats app_id:token header and validates profile status."""
        async with isolated_test_db(), suppress_email_notifications():
            cred = Credential(
                broker="FYERS", client_id="XR10079", api_key="APP123", token="RAW_TOK",
                static_ip="172.31.8.158", fingerprint="FYERS:XR10079", status="working"
            )
            self.assertEqual(get_fyers_auth_header(cred), "APP123:RAW_TOK")

            with patch("credentials.fyers.proxy_request", new=AsyncMock(return_value={"s": "ok", "code": 200})):
                self.assertEqual(await login_fyers(cred), "RAW_TOK")

            with patch("credentials.fyers.proxy_request", new=AsyncMock(return_value={"s": "error", "code": -17, "message": "Token expired"})):
                with self.assertRaises(ValueError) as ctx:
                    await login_fyers(cred)
                self.assertIn("Token expired", str(ctx.exception))

    async def test_kotakneo_headless_login_and_probe_recovery(self):
        """Kotak Neo performs headless 2-step login and recovers expired session probes."""
        async with isolated_test_db(), suppress_email_notifications():
            cred = Credential(
                broker="KOTAK NEO", client_id="VFIU2", api_key="k_tok", mobile_number="9876543210",
                mpin="123456", totp_secret="JBSWY3DPEHPK3PXP", static_ip="172.31.8.148",
                fingerprint="KOTAK NEO:VFIU2", status="working"
            )
            self.assertEqual(get_kotak_base_url(cred), "https://napi.kotaksecurities.com")
            self.assertEqual(get_kotak_headers(cred)["neo-fin-key"], "neotradeapi")

            with patch("credentials.kotakneo.proxy_request", new=AsyncMock(side_effect=[
                {"data": {"token": "tmp_k", "sid": "sid_1"}},
                {"data": {"token": "trade_k", "sid": "sid_2", "baseUrl": "https://gw.kotak.in"}},
                {"stat": "Ok", "data": []},
            ])):
                tok = await login_kotakneo(cred)
                self.assertEqual(tok, "trade_k")
                self.assertEqual(cred.sid, "sid_2")
                self.assertEqual(cred.base_url, "https://gw.kotak.in")

            # Missing credentials returns structured error dict
            bad_res = await generate_user_kotak_neo_access_token("VFIU2", "", "", "k_tok", "", "")
            self.assertFalse(bad_res["status"])

    async def test_axisdirect_crypto_roundtrip_and_missing_creds_error(self):
        """Axis Direct AES-GCM and RSA-OAEP helpers roundtrip cleanly and login guards missing keys."""
        secret = "0123456789abcdef"  # 16 bytes for AES-128-GCM
        encrypted = encrypt_aes('{"hello": "world"}', secret)
        self.assertEqual(decrypt_data(encrypted, secret), '{"hello": "world"}')

        key_pair = RSA.generate(2048)
        pub_pem = key_pair.publickey().export_key().decode("utf-8")
        rsa_b64 = encrypt_rsa(secret, pub_pem)
        self.assertTrue(len(rsa_b64) > 20)

        cred_bad = Credential(broker="AXIS DIRECT", client_id="11939", fingerprint="AXIS DIRECT:11939", status="working")
        with self.assertRaises(ValueError) as ctx:
            await login_axisdirect(cred_bad)
        self.assertIn("missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
