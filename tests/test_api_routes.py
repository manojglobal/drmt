"""Unit tests for API endpoints and authentication handlers."""

import asyncio
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch, AsyncMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from api.auth import routes as auth_routes
from api.middleware import rate_limit_middleware
from api.routes import routes as api_routes
from core.engine import core_engine
from credentials.crypto import hash_password
from database.db import db_pool, get_db
from logger.logger import logger_system
from models.schemas import Position
from position.blocklist import add_to_blocklist, clear_blocklist, is_blocked
from tests.helpers import FakeRedis, isolated_test_db, suppress_email_notifications


class TestApiRoutes(AioHTTPTestCase):
    """Test suite for HTTP API endpoints, session handling, and authentication."""

    async def setUpAsync(self):
        await super().setUpAsync()
        self.db_cm = isolated_test_db()
        await self.db_cm.__aenter__()

        # Insert a test user into the isolated database
        async with get_db() as conn:
            hashed_pw = hash_password("CorrectPassword123")
            await conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
                (1, "admin", hashed_pw),
            )
            await conn.commit()

    async def tearDownAsync(self):
        clear_blocklist()
        await self.db_cm.__aexit__(None, None, None)
        await super().tearDownAsync()

    async def get_application(self):
        """Constructs test aiohttp application with DRMT routes registered."""
        app = web.Application()
        app.add_routes(auth_routes)
        app.add_routes(api_routes)
        return app

    async def test_login_success_sets_cookie(self):
        """Verify POST /api/login with valid credentials returns 200 and sets session cookie."""
        resp = await self.client.post(
            "/api/login",
            json={"username": "admin", "password": "CorrectPassword123"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data.get("success"))
        self.assertIn("drmt_session", resp.cookies)

    async def test_login_invalid_password_returns_401(self):
        """Verify POST /api/login with invalid password returns 401."""
        resp = await self.client.post(
            "/api/login",
            json={"username": "admin", "password": "WrongPassword!"},
        )
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertFalse(data.get("success"))

    async def test_login_nonexistent_user_returns_401(self):
        """Verify POST /api/login with non-existent user returns 401."""
        resp = await self.client.post(
            "/api/login",
            json={"username": "nonexistent_user", "password": "any"},
        )
        self.assertEqual(resp.status, 401)

    async def test_api_me_unauthorized_without_cookie(self):
        """Verify GET /api/me returns 401 when accessed without authentication."""
        resp = await self.client.get("/api/me")
        self.assertEqual(resp.status, 401)

    async def test_api_me_authorized_with_login_cookie(self):
        """Verify GET /api/me returns current user info when valid session cookie is present."""
        # 1. Login
        login_resp = await self.client.post(
            "/api/login",
            json={"username": "admin", "password": "CorrectPassword123"},
        )
        session_cookie = login_resp.cookies.get("drmt_session").value

        # 2. Access /api/me with cookie
        resp = await self.client.get(
            "/api/me",
            cookies={"drmt_session": session_cookie},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data.get("user", {}).get("username"), "admin")

    async def test_get_blocked_users_endpoint(self):
        """Verify GET /api/blocked-users returns currently quarantined accounts."""
        # 1. Login
        login_resp = await self.client.post(
            "/api/login",
            json={"username": "admin", "password": "CorrectPassword123"},
        )
        session_cookie = login_resp.cookies.get("drmt_session").value

        # 2. Add an account to blocklist
        add_to_blocklist(
            client_id="FZ48062",
            broker="FLATTRADE",
            username="Manoj Flattrade",
            reason="Session token expired",
            signature="sig_test_123",
        )

        # 3. Query /api/blocked-users
        resp = await self.client.get(
            "/api/blocked-users",
            cookies={"drmt_session": session_cookie},
        )
        self.assertEqual(resp.status, 200)
        blocked_list = await resp.json()
        self.assertIsInstance(blocked_list, list)
        self.assertEqual(len(blocked_list), 1)
        self.assertEqual(blocked_list[0]["client_id"], "FZ48062")
        self.assertEqual(blocked_list[0]["reason"], "Session token expired")

    async def test_logout_clears_session(self):
        """Verify POST /api/logout terminates the active session."""
        login_resp = await self.client.post(
            "/api/login",
            json={"username": "admin", "password": "CorrectPassword123"},
        )
        session_cookie = login_resp.cookies.get("drmt_session").value

        logout_resp = await self.client.post(
            "/api/logout",
            cookies={"drmt_session": session_cookie},
        )
        self.assertEqual(logout_resp.status, 200)

        # Confirm session is invalidated
        me_resp = await self.client.get(
            "/api/me",
            cookies={"drmt_session": session_cookie},
        )
        self.assertEqual(me_resp.status, 401)

    async def test_auth_no_semaphore_deadlock_under_concurrency(self):
        """More than DB_POOL_SIZE concurrent invalid logins, invalid cookies, and expired sessions never deadlock."""
        past_dt = (datetime.now() - timedelta(hours=1)).isoformat()
        expired_tokens = [f"expired_tok_{i}" for i in range(8)]
        async with get_db() as db:
            for tok in expired_tokens:
                await db.execute(
                    "INSERT INTO sessions (user_id, token, expires_at) VALUES (?, ?, ?)",
                    (1, tok, past_dt),
                )
            await db.commit()

        async def _check_requests():
            tasks = [
                *(
                    self.client.post(
                        "/api/login",
                        json={"username": f"ghost_{i}", "password": "x"},
                    )
                    for i in range(8)
                ),
                *(
                    self.client.post(
                        "/api/login",
                        json={"username": "admin", "password": "WrongPassword!"},
                    )
                    for _ in range(6)
                ),
                *(
                    self.client.get(
                        "/api/me",
                        cookies={"drmt_session": f"invalid_tok_{i}"},
                    )
                    for i in range(8)
                ),
                *(
                    self.client.get(
                        "/api/me",
                        cookies={"drmt_session": tok},
                    )
                    for tok in expired_tokens
                ),
            ]
            responses = await asyncio.gather(*tasks)
            self.assertEqual(len(responses), 30)
            for resp in responses:
                self.assertEqual(resp.status, 401)

        await asyncio.wait_for(_check_requests(), timeout=25.0)

        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM sessions WHERE token LIKE 'expired_tok_%'")
            self.assertEqual((await cursor.fetchone())[0], 0)
            cursor = await db.execute(
                "SELECT action, COUNT(*) FROM auth_audit_logs GROUP BY action"
            )
            counts = {row[0]: row[1] for row in await cursor.fetchall()}
            self.assertEqual(counts.get("LOGIN_FAILED"), 14)
            self.assertEqual(counts.get("SESSION_INVALID"), 8)
            self.assertEqual(counts.get("SESSION_EXPIRED"), 8)

        self.assertEqual(db_pool._semaphore._value, db_pool.max_connections)

    async def _login_cookie(self) -> str:
        login_resp = await self.client.post(
            "/api/login",
            json={"username": "admin", "password": "CorrectPassword123"},
        )
        return login_resp.cookies["drmt_session"].value

    async def test_expired_session_rejected_and_audit_logged(self):
        """Expired session cookie returns 401 and audit-logs endpoint filters history."""
        cookie = await self._login_cookie()
        past_dt = (datetime.now() - timedelta(hours=1)).isoformat()
        async with get_db() as db:
            await db.execute("UPDATE sessions SET expires_at = ? WHERE token = ?", (past_dt, cookie))
            await db.commit()

        me_resp = await self.client.get("/api/me", cookies={"drmt_session": cookie})
        self.assertEqual(me_resp.status, 401)

        # Re-login and verify /api/audit-logs records both LOGIN_SUCCESS and SESSION_EXPIRED
        new_cookie = await self._login_cookie()
        audit_resp = await self.client.get("/api/audit-logs?limit=10", cookies={"drmt_session": new_cookie})
        self.assertEqual(audit_resp.status, 200)
        audit_data = await audit_resp.json()
        actions = {row["action"] for row in audit_data["data"]}
        self.assertIn("LOGIN_SUCCESS", actions)
        self.assertIn("SESSION_EXPIRED", actions)

    async def test_exit_endpoint_and_exited_history_queries(self):
        """POST /api/exit falls back ltp->avg_price, saves DB, syncs Redis, and GET /api/exited filters."""
        cookie = await self._login_cookie()
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token)
                VALUES ('FLATTRADE', 'FZ59716', 'ak', 'as', 'FLATTRADE:FZ59716', 'working', 'tok_1')
                """
            )
            await db.commit()

        core_engine.redis_pool = FakeRedis()
        await core_engine.redis_pool.set(
            "positions:1",
            json.dumps([
                {"id": "FLATTRADE:FZ59716:WIPRO-EQ", "symbol": "WIPRO-EQ"},
                {"id": "FLATTRADE:FZ59716:INFY-EQ", "symbol": "INFY-EQ"},
            ]),
        )

        # Empty positions returns message without error
        empty_resp = await self.client.post("/api/exit", json={"positions": []}, cookies={"drmt_session": cookie})
        self.assertEqual(empty_resp.status, 200)
        self.assertFalse((await empty_resp.json())["success"])

        pos_obj = Position(
            id="FLATTRADE:FZ59716:WIPRO-EQ", broker="FLATTRADE", client_id="FZ59716",
            symbol="WIPRO-EQ", token="3787", exchange="NSE", product_type="I",
            order_type="MKT", transaction_type="BUY", quantity=5, avg_price=210.0, ltp=0.0, pnl=0.0,
        )
        with (
            patch("api.routes.get_configured_client_ids", return_value={"FZ59716"}),
            patch("api.routes.mass_exit", new=AsyncMock(return_value=(1, [{
                "broker": "FLATTRADE", "symbol": "WIPRO-EQ", "success": True, "message": "OK", "position": pos_obj
            }]))),
        ):
            exit_resp = await self.client.post(
                "/api/exit",
                json={"positions": [{
                    "id": "FLATTRADE:FZ59716:WIPRO-EQ", "broker": "FLATTRADE", "client_id": "FZ59716",
                    "symbol": "WIPRO-EQ", "token": "3787", "exchange": "NSE", "product_type": "I",
                    "order_type": "MKT", "quantity": 5, "avg_price": 210.0, "ltp": 0.0, "pnl": 0.0,
                }]},
                cookies={"drmt_session": cookie},
            )
        self.assertEqual(exit_resp.status, 200)
        exit_json = await exit_resp.json()
        self.assertTrue(exit_json["success"])

        # Redis keeps only non-exited position
        remaining_redis = json.loads(await core_engine.redis_pool.get("positions:1"))
        self.assertEqual([p["id"] for p in remaining_redis], ["FLATTRADE:FZ59716:INFY-EQ"])
        core_engine.redis_pool = None

        dates_resp = await self.client.get("/api/exited/dates", cookies={"drmt_session": cookie})
        self.assertEqual(dates_resp.status, 200)
        self.assertTrue(len(await dates_resp.json()) >= 1)

        exited_resp = await self.client.get("/api/exited?broker=FLATTRADE&client_id=ALL", cookies={"drmt_session": cookie})
        exited_data = (await exited_resp.json())["data"]
        self.assertEqual(len(exited_data), 1)
        self.assertEqual(exited_data[0]["exit_price"], 210.0)
        self.assertEqual(exited_data[0]["product_type"], "INTRADAY")

    async def test_credentials_source_toggle_refresh_and_blocklist_endpoints(self):
        """Test source toggling, credential refresh, blocklist retry/sync-dropbox/refresh-token, logs, and market-close."""
        cookie = await self._login_cookie()
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO credentials (broker, client_id, api_key, api_secret, fingerprint, status, token)
                VALUES ('FLATTRADE', 'FZ59716', 'ak', 'as', 'FLATTRADE:FZ59716', 'working', 'old_tok')
                """
            )
            await db.commit()

        # 1. GET & POST /api/credentials/source
        with (
            patch("credentials.dynamic_loader.fetch_and_sync_credentials", new=AsyncMock(return_value=48)),
            patch("settings.settings.USE_DROPBOX_CREDENTIALS", False),
        ):
            src_get = await self.client.get("/api/credentials/source", cookies={"drmt_session": cookie})
            self.assertEqual(src_get.status, 200)
            src_post = await self.client.post(
                "/api/credentials/source",
                json={"use_dropbox": True},
                cookies={"drmt_session": cookie},
            )
            self.assertEqual(src_post.status, 200)
            self.assertTrue((await src_post.json())["use_dropbox"])

        # 2. POST /api/credentials/refresh
        async with suppress_email_notifications():
            with (
                patch("credentials.dynamic_loader.fetch_and_sync_credentials", new=AsyncMock(return_value=1)),
                patch("main.authenticate_configured_credentials", new=AsyncMock(return_value=(["FLATTRADE:FZ59716"], [], [{
                    "broker": "FLATTRADE", "client_id": "FZ59716", "user_email": "jagdeesh@gmail.com", "refreshed": True, "reason": ""
                }]))),
            ):
                ref_resp = await self.client.post("/api/credentials/refresh", cookies={"drmt_session": cookie})
                self.assertEqual(ref_resp.status, 200)
                self.assertEqual((await ref_resp.json())["refreshed"], 1)
                await asyncio.sleep(0)

        # 3. Blocked user endpoints: missing client_id -> 400, retry & refresh-token & sync-dropbox -> unblock
        bad_retry = await self.client.post("/api/blocked-users/retry", json={}, cookies={"drmt_session": cookie})
        self.assertEqual(bad_retry.status, 400)

        add_to_blocklist("FZ59716", "FLATTRADE", "Jagdeesh Sawant", "Token expired")
        dyn_cred = {
            "broker": "FLATTRADE", "client_id": "FZ59716", "password": "pwd", "api_key": "ak",
            "api_secret": "as", "totp_secret": "TOTP", "static_ip": "1.1.1.1", "token": "tok_new"
        }
        async def _ok_refresh(cred, send_email=True):
            cred.token = "tok_refreshed_123"
            return True

        async with suppress_email_notifications():
            with (
                patch("credentials.dynamic_loader.fetch_and_sync_credentials", new=AsyncMock(return_value=1)),
                patch("api.routes.get_credentials_from_static_csv", return_value=dyn_cred),
                patch("api.routes.get_dynamic_credentials", return_value=dyn_cred),
                patch("api.routes.refresh_token_with_backoff", side_effect=_ok_refresh),
            ):
                rt_resp = await self.client.post(
                    "/api/blocked-users/refresh-token",
                    json={"client_id": "FZ59716"},
                    cookies={"drmt_session": cookie},
                )
                self.assertEqual(rt_resp.status, 200)
                self.assertFalse((await rt_resp.json())["blocked"])
                self.assertFalse(is_blocked("FZ59716"))

                add_to_blocklist("FZ59716", "FLATTRADE", "Jagdeesh Sawant", "Token expired")
                sync_resp = await self.client.post("/api/blocked-users/sync-dropbox", cookies={"drmt_session": cookie})
                self.assertEqual(sync_resp.status, 200)
                self.assertEqual(len((await sync_resp.json())["unblocked"]), 1)

        # 4. Logs & market-close endpoints
        await logger_system.info("Test system entry")
        logs_resp = await self.client.get("/api/logs?type=system", cookies={"drmt_session": cookie})
        self.assertEqual(logs_resp.status, 200)
        self.assertTrue(len(await logs_resp.json()) >= 1)

        with patch("credentials.scheduler.execute_market_close_email_report", new=AsyncMock(return_value={"successful_count": 1, "failed_count": 0})):
            mc_resp = await self.client.post("/api/reports/market-close", cookies={"drmt_session": cookie})
            self.assertEqual(mc_resp.status, 200)
            self.assertTrue((await mc_resp.json())["success"])

    async def test_rate_limit_middleware_invokes_rate_limiter(self):
        """rate_limit_middleware enforces per-user/IP rate limiter and passes normal requests."""
        app = web.Application(middlewares=[rate_limit_middleware])
        app.router.add_get("/api/ping", lambda req: web.json_response({"ok": True}))
        with patch.object(core_engine, "acquire_rate_limit", new=AsyncMock()) as mock_rl:
            from aiohttp.test_utils import TestClient, TestServer
            async with TestClient(TestServer(app)) as tc:
                r = await tc.get("/api/ping")
                self.assertEqual(r.status, 200)
                mock_rl.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
