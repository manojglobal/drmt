"""Reusable test helpers, mocks, and temporary fixtures for unit tests."""

from contextlib import asynccontextmanager
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

from database.db import db_pool, init_db


class MockHttpResponse:
    """Mock aiohttp ClientResponse object."""

    def __init__(self, data: Any, status: int = 200, text_data: Optional[str] = None):
        self._data = data
        self.status = status
        self._text_data = text_data or (str(data) if not isinstance(data, (dict, list)) else "")

    async def json(self, content_type: Optional[str] = None) -> Any:
        return self._data

    async def text(self) -> str:
        return self._text_data

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"Mock HTTP Error: {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockClientSession:
    """Mock aiohttp ClientSession object for routing requests to fixtures."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None):
        self.routes = routes or {}
        self.closed = False

    def get(self, url: str, **kwargs):
        resp_data = self.routes.get(url, {"stat": "ok"})
        status = 200
        if isinstance(resp_data, tuple):
            resp_data, status = resp_data
        return MockHttpResponse(resp_data, status=status)

    def post(self, url: str, **kwargs):
        resp_data = self.routes.get(url, {"stat": "ok"})
        status = 200
        if isinstance(resp_data, tuple):
            resp_data, status = resp_data
        return MockHttpResponse(resp_data, status=status)

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


@asynccontextmanager
async def isolated_test_db():
    """
    Context manager providing an isolated temporary SQLite database.
    Temporarily overrides `db_pool.db_path` and initializes all tables and migrations.
    Restores the original database path upon exit.
    """
    original_path = db_pool.db_path
    original_init = db_pool._initialized

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        temp_db_path = tf.name

    try:
        db_pool.db_path = temp_db_path
        db_pool._initialized = False
        await init_db()
        yield temp_db_path
    finally:
        db_pool.db_path = original_path
        db_pool._initialized = original_init
        if os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except OSError:
                pass


class FakeRedis:
    """In-memory async Redis stand-in for testing snapshot diffing and instrument caches."""

    def __init__(self):
        self.kv: Dict[str, str] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.ttls: Dict[str, int] = {}

    async def get(self, key: str) -> Optional[str]:
        return self.kv.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.kv[key] = value
        return True

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, mapping: Dict[str, str]) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in mapping.items()})
        return len(mapping)

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def flushdb(self) -> bool:
        self.kv.clear()
        self.hashes.clear()
        self.ttls.clear()
        return True

    async def close(self) -> None:
        return None


class FakeWebSocket:
    """In-memory WebSocket stand-in capturing pushed frames and close events."""

    def __init__(self, fail_send: bool = False):
        self.sent_bytes: List[bytes] = []
        self.closed = False
        self.fail_send = fail_send

    async def send_bytes(self, data: bytes) -> None:
        if self.fail_send:
            raise ConnectionError("WebSocket send failed")
        self.sent_bytes.append(data)

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def isolated_log_dir():
    """Redirects logger.logger._LOG_BASE_DIR to a temporary directory for safe file-rotation tests."""
    import logger.logger as log_mod

    original_dir = log_mod._LOG_BASE_DIR
    temp_dir = tempfile.mkdtemp(prefix="drmt_test_logs_")
    try:
        log_mod._LOG_BASE_DIR = temp_dir
        yield temp_dir
    finally:
        log_mod._LOG_BASE_DIR = original_dir
        shutil.rmtree(temp_dir, ignore_errors=True)


@asynccontextmanager
async def suppress_email_notifications():
    """Mocks utils.email.send_error_email and logger.logger.send_error_email to keep tests isolated."""
    mock_send = AsyncMock()
    with (
        patch("utils.email.send_error_email", mock_send),
        patch("logger.logger.send_error_email", mock_send),
    ):
        yield mock_send
