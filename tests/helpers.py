"""Reusable test helpers, mocks, and temporary fixtures for unit tests."""

import os
import tempfile
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
import aiosqlite

from database.db import DatabasePool, init_db, db_pool
from settings import settings


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
