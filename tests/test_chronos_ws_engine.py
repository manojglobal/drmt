"""Unit tests for CoreEngine, Chronos polling loop, and WebSocket handler."""

import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
import zoneinfo

from aiohttp import WSMsgType, web
from aiohttp.test_utils import AioHTTPTestCase

from api.auth import routes as auth_routes
from api.ws import websocket_handler
from core.engine import CoreEngine, core_engine
from credentials.crypto import hash_password
from database.db import get_db
from execution.engine import _recently_exited_positions, mark_position_as_exited
from models.schemas import Credential, Position
from position.blocklist import clear_blocklist, is_blocked
from position.chronos import polling_loop
from tests.helpers import FakeRedis, FakeWebSocket, isolated_test_db, suppress_email_notifications

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")


class TestChronosAndWebSocketEngine(unittest.IsolatedAsyncioTestCase):
    """Tests CoreEngine worker queue, Chronos Redis diffing, anti-echo, zombie timeout, and error isolation."""

    async def asyncSetUp(self):
        clear_blocklist()
        _recently_exited_positions.clear()

    async def asyncTearDown(self):
        clear_blocklist()
        _recently_exited_positions.clear()

    async def test_core_engine_async_submit_and_stop(self):
        """CoreEngine worker loop executes submitted coroutines, propagates errors, and cleans up on stop."""
        engine = CoreEngine()
        with patch("core.engine.redis.from_url", return_value=FakeRedis()):
            await engine.start()
        try:
            async def _add(x, y):
                return x + y

            fut = await engine.async_submit(_add(2, 3))
            self.assertEqual(await fut, 5)

            async def _boom():
                raise ValueError("worker boom")

            fut_err = await engine.async_submit(_boom())
            with self.assertRaises(ValueError) as ctx:
                await fut_err
            self.assertIn("worker boom", str(ctx.exception))
        finally:
            await engine.stop()
            self.assertTrue(engine.http_session.closed)

    async def test_core_engine_stop_deletes_only_position_keys_and_preserves_instruments(self):
        """CoreEngine.stop() deletes only positions:* keys while preserving instrument caches and unrelated keys."""
        fake_redis = FakeRedis()
        engine = CoreEngine()
        with patch("core.engine.redis.from_url", return_value=fake_redis):
            await engine.start()

        await fake_redis.set("positions:1", json.dumps([{"id": "p1"}]))
        await fake_redis.set("positions:205", json.dumps([{"id": "p2"}]))
        await fake_redis.set("positions_other:1", "keep-me")
        await fake_redis.hset("axis:instrument_tokens", {"TCS": "123"})
        await fake_redis.expire("axis:instrument_tokens", 86400)
        await fake_redis.hset("kotak:symbols", {"NSE:INFY": "456"})
        await fake_redis.hset("mstock:token_to_tsym", {"NSE:1": "WIPRO-EQ"})

        await engine.stop()

        self.assertIsNone(await fake_redis.get("positions:1"))
        self.assertIsNone(await fake_redis.get("positions:205"))
        self.assertEqual(await fake_redis.get("positions_other:1"), "keep-me")
        self.assertEqual(await fake_redis.hgetall("axis:instrument_tokens"), {"TCS": "123"})
        self.assertEqual(fake_redis.ttls.get("axis:instrument_tokens"), 86400)
        self.assertEqual(await fake_redis.hgetall("kotak:symbols"), {"NSE:INFY": "456"})
        self.assertEqual(await fake_redis.hgetall("mstock:token_to_tsym"), {"NSE:1": "WIPRO-EQ"})
        self.assertTrue(fake_redis.closed)

    async def test_core_engine_stop_closes_pool_when_redis_scan_fails(self):
        """CoreEngine.stop() catches scan_iter errors and still closes redis_pool cleanly."""
        class _FailingScanRedis:
            def __init__(self):
                self.close = AsyncMock()

            async def scan_iter(self, match=None):
                raise ConnectionError("redis unreachable")
                yield match  # pragma: no cover

        failing_redis = _FailingScanRedis()
        engine = CoreEngine()
        with patch("core.engine.redis.from_url", return_value=failing_redis):
            await engine.start()

        await engine.stop()
        failing_redis.close.assert_awaited_once()

    async def test_chronos_polling_loop_survives_redis_failure_or_none(self):
        """polling_loop streams positions and deduplicates snapshots even if redis_pool is None or failing."""
        class _BrokenRedis:
            async def get(self, key):
                raise ConnectionError("redis read down")

            async def set(self, key, val):
                raise ConnectionError("redis write down")

        today_dt = datetime.now(tz=_IST)
        cred = Credential(
            broker="FLATTRADE", client_id="FZ59716", token="tok_today",
            token_updated_at=today_dt, fingerprint="FLATTRADE:FZ59716", status="working"
        )
        pos_live = Position(
            id="FLATTRADE:FZ59716:WIPRO-EQ:NSE:INTRADAY", broker="FLATTRADE", client_id="FZ59716",
            symbol="WIPRO-EQ", token="3787", exchange="NSE", product_type="INTRADAY",
            order_type="MKT", transaction_type="BUY", quantity=5, avg_price=200.0, ltp=205.0, pnl=25.0
        )

        for pool in (None, _BrokenRedis()):
            engine = CoreEngine()
            engine.redis_pool = pool
            ws = FakeWebSocket()
            sleep_calls = 0

            async def _fake_sleep(secs):
                nonlocal sleep_calls
                sleep_calls += 1
                if sleep_calls >= 3:
                    engine.last_ping["205"] = time.monotonic() - 60.0

            with (
                patch("position.chronos.is_before_daily_refresh", return_value=False),
                patch("position.chronos.is_morning_refresh_running", return_value=False),
                patch("position.chronos.random.uniform", return_value=0.0),
                patch("position.chronos.asyncio.sleep", side_effect=_fake_sleep),
                patch("position.chronos.broker_poll", new=AsyncMock(return_value=[pos_live])),
            ):
                await polling_loop("205", ws, [cred], engine)

            self.assertEqual(len(ws.sent_bytes), 1)
            msg = json.loads(ws.sent_bytes[0].decode("utf-8"))
            self.assertEqual([p["symbol"] for p in msg["data"]], ["WIPRO-EQ"])
            self.assertTrue(ws.closed)

    async def test_chronos_polling_loop_hydrates_redis_diffs_and_filters_exited(self):
        """polling_loop sends cached snapshot immediately, strips recently exited positions, and pushes diffs only."""
        engine = CoreEngine()
        engine.redis_pool = FakeRedis()
        cached_pos = [{
            "id": "FLATTRADE:FZ59716:OLD-EQ", "broker": "FLATTRADE", "client_id": "FZ59716",
            "username": "Jagdeesh Sawant", "symbol": "OLD-EQ", "token": "1", "exchange": "NSE",
            "product_type": "INTRADAY", "order_type": "MKT", "transaction_type": "BUY",
            "quantity": 1, "avg_price": 100.0, "ltp": 101.0, "pnl": 1.0, "cash": 100.0, "cash_value": 100.0,
        }]
        await engine.redis_pool.set("positions:205", json.dumps(cached_pos))
        ws = FakeWebSocket()

        today_dt = datetime.now(tz=_IST)
        cred = Credential(
            broker="FLATTRADE", client_id="FZ59716", token="tok_today",
            token_updated_at=today_dt, fingerprint="FLATTRADE:FZ59716", status="working"
        )
        pos_live = Position(
            id="FLATTRADE:FZ59716:WIPRO-EQ:NSE:INTRADAY", broker="FLATTRADE", client_id="FZ59716",
            symbol="WIPRO-EQ", token="3787", exchange="NSE", product_type="INTRADAY",
            order_type="MKT", transaction_type="BUY", quantity=5, avg_price=200.0, ltp=205.0, pnl=25.0
        )
        pos_echo = Position(
            id="FLATTRADE:FZ59716:RELIANCE-EQ:NSE:INTRADAY", broker="FLATTRADE", client_id="FZ59716",
            symbol="RELIANCE-EQ", token="2885", exchange="NSE", product_type="INTRADAY",
            order_type="MKT", transaction_type="BUY", quantity=2, avg_price=2500.0, ltp=2510.0, pnl=20.0
        )
        pos_cnc_sibling = Position(
            id="FLATTRADE:FZ59716:RELIANCE-EQ:NSE:CNC", broker="FLATTRADE", client_id="FZ59716",
            symbol="RELIANCE-EQ", token="2885", exchange="NSE", product_type="CNC",
            order_type="MKT", transaction_type="BUY", quantity=1, avg_price=2500.0, ltp=2510.0, pnl=10.0
        )
        mark_position_as_exited(pos_echo.id)

        sleep_calls = 0

        async def _fake_sleep(secs):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                # Force zombie timeout after 2 full poll cycles
                engine.last_ping["205"] = time.monotonic() - 60.0

        with (
            patch("position.chronos.is_before_daily_refresh", return_value=False),
            patch("position.chronos.is_morning_refresh_running", return_value=False),
            patch("position.chronos.random.uniform", return_value=0.0),
            patch("position.chronos.asyncio.sleep", side_effect=_fake_sleep),
            patch("position.chronos.broker_poll", new=AsyncMock(return_value=[pos_live, pos_echo, pos_cnc_sibling])),
        ):
            await polling_loop("205", ws, [cred], engine)

        # Frame 1: cached Redis snapshot; Frame 2: WIPRO-EQ + RELIANCE-EQ (CNC) preserved, INTRADAY echo filtered.
        # Cycle 2 has identical snapshot so diff check skips Frame 3.
        self.assertEqual(len(ws.sent_bytes), 2)
        msg0 = json.loads(ws.sent_bytes[0].decode("utf-8"))
        msg1 = json.loads(ws.sent_bytes[1].decode("utf-8"))
        self.assertEqual(msg0["data"][0]["symbol"], "OLD-EQ")
        self.assertEqual(
            [p["id"] for p in msg1["data"]],
            ["FLATTRADE:FZ59716:WIPRO-EQ:NSE:INTRADAY", "FLATTRADE:FZ59716:RELIANCE-EQ:NSE:CNC"],
        )
        self.assertTrue(ws.closed)

    async def test_chronos_quarantines_expired_token_and_ignores_transient_502(self):
        """Expired token raises PermissionError -> blocklist; transient 502 error does NOT blocklist."""
        engine = CoreEngine()
        engine.redis_pool = FakeRedis()
        ws = FakeWebSocket()

        yesterday = datetime.now(tz=_IST) - timedelta(days=1)
        today = datetime.now(tz=_IST)
        cred_expired = Credential(
            broker="FLATTRADE", client_id="C_EXP", token="old_tok",
            token_updated_at=yesterday, fingerprint="FLATTRADE:C_EXP", status="working"
        )
        cred_502 = Credential(
            broker="MSTOCK", client_id="C_502", token="ok_tok",
            token_updated_at=today, fingerprint="MSTOCK:C_502", status="working"
        )

        async def _fake_sleep(secs):
            if secs >= 1.0:
                engine.last_ping["205"] = time.monotonic() - 60.0

        async with suppress_email_notifications():
            with (
                patch("position.chronos.is_before_daily_refresh", return_value=False),
                patch("position.chronos.is_morning_refresh_running", return_value=False),
                patch("position.chronos.random.uniform", return_value=0.0),
                patch("position.chronos.asyncio.sleep", side_effect=_fake_sleep),
                patch("position.chronos.broker_poll", new=AsyncMock(side_effect=RuntimeError("502 Bad Gateway"))),
            ):
                await polling_loop("205", ws, [cred_expired, cred_502], engine)

        self.assertTrue(is_blocked("C_EXP"))
        self.assertFalse(is_blocked("C_502"))

    async def test_chronos_suppresses_auth_errors_before_0910_and_during_morning_refresh(self):
        """Pre-09:10 IST and active morning refresh defer all auth quarantine, warnings, and emails."""
        yesterday = datetime.now(tz=_IST) - timedelta(days=1)
        today = datetime.now(tz=_IST)

        for before_flag, running_flag in [(True, False), (False, True)]:
            clear_blocklist()
            engine = CoreEngine()
            engine.redis_pool = FakeRedis()
            ws = FakeWebSocket()

            creds = [
                Credential(
                    broker="FLATTRADE", client_id="FZ58776", token="t1", token_updated_at=today,
                    fingerprint="FLATTRADE:FZ58776", status="working",
                ),
                Credential(
                    broker="MSTOCK", client_id="C_EXP", token="old_tok", token_updated_at=yesterday,
                    fingerprint="MSTOCK:C_EXP", status="working",
                ),
                Credential(
                    broker="ALICE BLUE", client_id="C_REJECT", token="tok_today", token_updated_at=today,
                    fingerprint="ALICE BLUE:C_REJECT", status="working",
                ),
            ]

            async def _fake_sleep(secs):
                if secs >= 1.0:
                    engine.last_ping["205"] = time.monotonic() - 60.0

            mock_send = AsyncMock()
            mock_auth_warn = AsyncMock()
            with (
                patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"),
                patch("position.chronos.is_before_daily_refresh", return_value=before_flag),
                patch("position.chronos.is_morning_refresh_running", return_value=running_flag),
                patch("position.chronos.random.uniform", return_value=0.0),
                patch("position.chronos.asyncio.sleep", side_effect=_fake_sleep),
                patch("position.chronos.broker_poll", new=AsyncMock(side_effect=PermissionError("Unauthorized"))),
                patch("position.chronos.logger_auth.warn", mock_auth_warn),
                patch("utils.email.send_error_email", mock_send),
            ):
                await polling_loop("205", ws, creds, engine)
                await asyncio.sleep(0)

            self.assertFalse(is_blocked("FZ58776"))
            self.assertFalse(is_blocked("C_EXP"))
            self.assertFalse(is_blocked("C_REJECT"))
            mock_auth_warn.assert_not_awaited()
            mock_send.assert_not_awaited()

    async def test_chronos_merges_multiple_flattrade_missing_key_alerts_into_one_email(self):
        """After 09:10 IST, multiple Flattrade SDK accounts missing API keys produce 1 consolidated auth failure email."""
        from credentials.flattrade import FLATTRADE_MISSING_KEYS_MESSAGE, reset_missing_keys_alert_cache
        from position.blocklist import get_blocked_user

        reset_missing_keys_alert_cache()
        engine = CoreEngine()
        engine.redis_pool = FakeRedis()
        ws = FakeWebSocket()
        today = datetime.now(tz=_IST)

        c1 = Credential(
            broker="FLATTRADE", client_id="FZ58776", token="t1", token_updated_at=today,
            fingerprint="FLATTRADE:FZ58776", status="working",
        )
        c2 = Credential(
            broker="FLATTRADE", client_id="FZ48062", token="t2", token_updated_at=today,
            fingerprint="FLATTRADE:FZ48062", status="working",
        )

        async def _fake_sleep(secs):
            if secs >= 1.0:
                engine.last_ping["205"] = time.monotonic() - 60.0

        mock_send = AsyncMock()
        with (
            patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"),
            patch("position.chronos.is_before_daily_refresh", return_value=False),
            patch("position.chronos.is_morning_refresh_running", return_value=False),
            patch("position.chronos.random.uniform", return_value=0.0),
            patch("position.chronos.asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "position.chronos.broker_poll",
                new=AsyncMock(side_effect=PermissionError("Broker Auth Error (HTTP 401): {'status': 'E', 'errMsg': 'YOU ARE NOT AUTHORIZED', 'statusCode': 'MAIN01'}")),
            ),
            patch("credentials.dynamic_loader.get_client_email", side_effect=lambda cid: f"{cid.lower()}@gmail.com"),
            patch("utils.email.send_error_email", mock_send),
        ):
            await polling_loop("205", ws, [c1, c2], engine)
            email_tasks = [t for t in asyncio.all_tasks() if t.get_name() == "email_poll_expired_consolidated"]
            if email_tasks:
                await asyncio.gather(*email_tasks)

        self.assertTrue(is_blocked("FZ58776"))
        self.assertTrue(is_blocked("FZ48062"))
        self.assertEqual(get_blocked_user("FZ58776")["reason"], FLATTRADE_MISSING_KEYS_MESSAGE)
        self.assertEqual(mock_send.await_count, 1)
        subj, plain_body = mock_send.await_args.args[:2]
        self.assertIn("2 Credential(s) Failed Authentication", subj)
        self.assertIn(FLATTRADE_MISSING_KEYS_MESSAGE, plain_body)
        self.assertIn("FZ58776", plain_body)
        self.assertIn("FZ48062", plain_body)

    async def test_flattrade_401_poll_sends_only_consolidated_html_email_no_api_emails(self):
        """HTTP 401 via proxy -> _poll_flattrade_sdk -> polling_loop sends only 1 consolidated HTML email."""
        import utils.email as email_mod
        from tests.helpers import isolated_log_dir

        class _MockProxyResp:
            status = 200
            async def json(self, content_type=None):
                return {
                    "status_code": 401,
                    "json": {"stat": "Not_Ok", "emsg": "Session Expired : Invalid Session Key"},
                }
            def raise_for_status(self):
                return None
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _MockProxySession:
            def post(self, url, **kwargs):
                return _MockProxyResp()

        email_mod._RECENT_SUBJECTS.clear()
        email_mod._EMAIL_SEMAPHORE = asyncio.Semaphore(1)
        dispatched = []

        def _capture_sync(subject, plain_body, html_body, *args):
            dispatched.append((subject, plain_body, html_body))

        engine = CoreEngine()
        engine.redis_pool = FakeRedis()
        ws = FakeWebSocket()
        today = datetime.now(tz=_IST)
        cred = Credential(
            broker="FLATTRADE", client_id="FZ59716", api_key="ak", api_secret="as",
            static_ip="172.31.8.133", token="expired_tok", token_updated_at=today,
            fingerprint="FLATTRADE:FZ59716", status="working",
        )

        async def _fake_sleep(secs):
            if secs >= 1.0:
                engine.last_ping["205"] = time.monotonic() - 60.0

        prev_session = core_engine.http_session
        core_engine.http_session = _MockProxySession()
        try:
            async with isolated_log_dir():
                with (
                    patch("position.adapters.settings.FLATTRADE_FLOW", "SDK"),
                    patch("credentials.flattrade.settings.FLATTRADE_FLOW", "SDK"),
                    patch("position.chronos.is_before_daily_refresh", return_value=False),
                    patch("position.chronos.is_morning_refresh_running", return_value=False),
                    patch("position.chronos.random.uniform", return_value=0.0),
                    patch("position.chronos.asyncio.sleep", side_effect=_fake_sleep),
                    patch("utils.user_mapping.get_client_username", return_value="Jagdeesh Sawant"),
                    patch("position.chronos.get_client_username", return_value="Jagdeesh Sawant"),
                    patch("credentials.dynamic_loader.get_client_email", return_value="jagdeesh@gmail.com"),
                    patch("utils.email.settings.EMAIL_USER", "sender@example.com"),
                    patch("utils.email.settings.EMAIL_PASS", "smtp_pass"),
                    patch("utils.email.settings.EMAIL_TO", "ops@example.com"),
                    patch("utils.email._send_email_sync", side_effect=_capture_sync),
                ):
                    await polling_loop("205", ws, [cred], engine)
                    email_tasks = [
                        t for t in asyncio.all_tasks()
                        if t.get_name() == "email_poll_expired_consolidated" or t.get_name().startswith("err_email_")
                    ]
                    if email_tasks:
                        await asyncio.gather(*email_tasks)
        finally:
            core_engine.http_session = prev_session

        self.assertTrue(is_blocked("FZ59716"))
        self.assertEqual(len(dispatched), 1)
        subj, _, html_body = dispatched[0]
        self.assertIn("Live Polling Session Expired: 1 Credential(s) Failed Authentication", subj)
        self.assertNotIn("DRMT ERROR [api]", subj)
        self.assertIn("<table", html_body)
        self.assertIn("FZ59716", html_body)
        self.assertIn("Jagdeesh Sawant", html_body)


class TestWebSocketRoutes(AioHTTPTestCase):
    """Tests /ws/positions authentication, TEXT/BINARY ping handling, and disconnect cleanup."""

    async def setUpAsync(self):
        await super().setUpAsync()
        self.db_cm = isolated_test_db()
        await self.db_cm.__aenter__()
        async with get_db() as conn:
            await conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
                (1, "admin", hash_password("Pass123")),
            )
            await conn.commit()

    async def tearDownAsync(self):
        await self.db_cm.__aexit__(None, None, None)
        await super().tearDownAsync()

    async def get_application(self):
        app = web.Application()
        app.add_routes(auth_routes)
        app.router.add_get("/ws/positions", websocket_handler)
        return app

    async def test_ws_rejects_unauthenticated_client(self):
        """GET /ws/positions without session cookie returns 401 Unauthorized."""
        resp = await self.client.get("/ws/positions")
        self.assertEqual(resp.status, 401)

    async def test_ws_accepts_authenticated_pings_and_cleans_up_on_close(self):
        """Authenticated WS handles TEXT and BINARY ping frames and removes polling task on disconnect."""
        login_resp = await self.client.post("/api/login", json={"username": "admin", "password": "Pass123"})
        self.assertEqual(login_resp.status, 200)
        cookie = login_resp.cookies["drmt_session"].value

        fake_task = asyncio.create_task(asyncio.sleep(60))
        with (
            patch.object(core_engine, "spawn_user", new=AsyncMock(return_value=fake_task)) as mock_spawn,
            patch.object(core_engine, "remove_user") as mock_remove,
        ):
            async with self.client.ws_connect("/ws/positions", headers={"Cookie": f"drmt_session={cookie}"}) as ws:
                msg = await ws.receive(timeout=2.0)
                self.assertEqual(msg.type, WSMsgType.BINARY)
                self.assertEqual(json.loads(msg.data.decode("utf-8")), {"type": "status", "connected": True})

                core_engine.last_ping["1"] = 0.0
                await ws.send_str(json.dumps({"type": "ping"}))
                await asyncio.sleep(0.05)
                self.assertGreater(core_engine.last_ping["1"], 0.0)

                core_engine.last_ping["1"] = 0.0
                await ws.send_bytes(json.dumps({"type": "ping"}).encode("utf-8"))
                await asyncio.sleep(0.05)
                self.assertGreater(core_engine.last_ping["1"], 0.0)
                await ws.close()

            mock_spawn.assert_awaited_once()
            mock_remove.assert_called_once_with("1", task=fake_task)
        if not fake_task.done():
            fake_task.cancel()


if __name__ == "__main__":
    unittest.main()
