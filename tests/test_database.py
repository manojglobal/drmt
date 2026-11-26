"""Unit tests for database/db.py and SQLite operations."""

import asyncio
from datetime import datetime
import unittest

from database.db import DatabasePool, db_pool, get_db
from tests.helpers import isolated_test_db


class TestDatabaseOperations(unittest.IsolatedAsyncioTestCase):
    """Test suite for database schema initialization, migrations, and CRUD operations."""

    async def test_database_initialization_and_tables(self):
        """Verify all core tables are created properly in isolated test database."""
        async with isolated_test_db():
            async with get_db() as conn:
                cursor = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                )
                tables = [row[0] for row in await cursor.fetchall()]
                self.assertIn("credentials", tables)
                self.assertIn("users", tables)
                self.assertIn("exited_positions", tables)
                self.assertIn("auth_audit_logs", tables)

    async def test_credential_insert_and_conflict_update(self):
        """Verify inserting credentials and upserting on fingerprint conflict."""
        async with isolated_test_db():
            async with get_db() as conn:
                # 1. Insert initial credential
                await conn.execute(
                    """
                    INSERT INTO credentials 
                    (broker, client_id, api_key, api_secret, fingerprint, status, token, token_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    ("FLATTRADE", "FZ48062", "", "", "FLATTRADE:FZ48062", "working", "token_v1"),
                )
                await conn.commit()

                # Verify inserted
                cursor = await conn.execute(
                    "SELECT token, status FROM credentials WHERE fingerprint = ?",
                    ("FLATTRADE:FZ48062",),
                )
                row = await cursor.fetchone()
                self.assertEqual(row[0], "token_v1")
                self.assertEqual(row[1], "working")

                # 2. Upsert with updated token
                await conn.execute(
                    """
                    INSERT INTO credentials 
                    (broker, client_id, api_key, api_secret, fingerprint, status, token, token_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        token = excluded.token,
                        token_updated_at = excluded.token_updated_at
                    """,
                    ("FLATTRADE", "FZ48062", "", "", "FLATTRADE:FZ48062", "working", "token_v2"),
                )
                await conn.commit()

                # Verify updated
                cursor = await conn.execute(
                    "SELECT token FROM credentials WHERE fingerprint = ?",
                    ("FLATTRADE:FZ48062",),
                )
                row = await cursor.fetchone()
                self.assertEqual(row[0], "token_v2")

    async def test_exited_positions_crud(self):
        """Verify recording and querying exited positions."""
        async with isolated_test_db():
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO exited_positions
                    (position_id, broker, client_id, symbol, token, exchange, product_type, order_type, transaction_type, quantity, avg_price, exit_price, pnl)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "FLATTRADE:FZ48062:WIPRO-EQ",
                        "FLATTRADE",
                        "FZ48062",
                        "WIPRO-EQ",
                        "3787",
                        "NSE",
                        "INTRADAY",
                        "MARKET",
                        "SELL",
                        -1,
                        193.73,
                        193.30,
                        0.43,
                    ),
                )
                await conn.commit()

                cursor = await conn.execute(
                    "SELECT symbol, quantity, transaction_type, exit_price, pnl FROM exited_positions WHERE client_id = ?",
                    ("FZ48062",),
                )
                row = await cursor.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "WIPRO-EQ")
                self.assertEqual(row[1], -1)
                self.assertEqual(row[2], "SELL")
                self.assertEqual(row[3], 193.30)
                self.assertAlmostEqual(row[4], 0.43)

    async def test_concurrent_db_connections(self):
        """Verify multiple async tasks can access the database pool concurrently."""
        async with isolated_test_db():
            async def worker(task_id: int):
                async with get_db() as conn:
                    await conn.execute(
                        "INSERT INTO auth_audit_logs (username, action, status) VALUES (?, ?, ?)",
                        (f"user_{task_id}", "LOGIN", "SUCCESS"),
                    )
                    await conn.commit()

            # Run 10 concurrent writes
            await asyncio.gather(*(worker(i) for i in range(10)))

            async with get_db() as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM auth_audit_logs")
                count = (await cursor.fetchone())[0]
                self.assertEqual(count, 10)

    async def test_reentrant_get_db_raises_without_deadlock(self):
        """Same-task nested get_db() fails fast with RuntimeError while child tasks still work."""
        async with isolated_test_db():
            async def child_read():
                async with get_db() as child_conn:
                    cursor = await child_conn.execute("SELECT 1")
                    return (await cursor.fetchone())[0]

            async with get_db():
                with self.assertRaises(RuntimeError):
                    async with get_db():
                        pass

                child_result = await asyncio.wait_for(asyncio.create_task(child_read()), timeout=2.0)
                self.assertEqual(child_result, 1)

            self.assertEqual(db_pool._semaphore._value, db_pool.max_connections)

    async def test_semaphore_acquire_timeout_preserves_pool_permits(self):
        """Timed-out semaphore acquisition does not leak or corrupt available pool permits."""
        async with isolated_test_db() as temp_db_path:
            small_pool = DatabasePool(temp_db_path, max_connections=1, acquire_timeout=0.05)

            async def contend():
                async with small_pool.get_connection() as conn:
                    cursor = await conn.execute("SELECT 1")
                    return (await cursor.fetchone())[0]

            async with small_pool.get_connection():
                with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
                    await asyncio.create_task(contend())

            self.assertEqual(small_pool._semaphore._value, 1)
            self.assertEqual(await asyncio.create_task(contend()), 1)
            self.assertEqual(small_pool._semaphore._value, 1)


if __name__ == "__main__":
    unittest.main()
