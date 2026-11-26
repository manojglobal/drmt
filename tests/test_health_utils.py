"""Unit tests for rate limiter, health monitor, and user mapping utilities."""

import unittest
from unittest.mock import patch, AsyncMock
import asyncio

from core.rate_limiter import TokenBucketRateLimiter
from health.monitor import HealthMonitor
from utils.user_mapping import (
    get_client_username,
    get_configured_client_ids,
)
from tests.helpers import isolated_test_db


class TestHealthAndUtilities(unittest.IsolatedAsyncioTestCase):
    """Test suite for rate limiting, health monitoring, and username mapping."""

    async def test_token_bucket_rate_limiter_burst_and_refill(self):
        """Verify token bucket rate limiter acquires within capacity and waits when exhausted."""
        # Limiter with 5 tokens capacity, 10 tokens per second refill
        limiter = TokenBucketRateLimiter(rate=10.0, capacity=5)
        self.assertEqual(limiter.tokens, 5)

        # Acquire all 5 initial tokens without delay
        for _ in range(5):
            await limiter.acquire()
        self.assertAlmostEqual(limiter.tokens, 0.0, delta=0.5)

        # Sleep 0.2s -> should refill ~2 tokens (0.2 * 10 = 2)
        await asyncio.sleep(0.2)
        await limiter.acquire()
        await limiter.acquire()

    def test_user_mapping_fallback_resolution(self):
        """Verify get_client_username resolves known defaults and falls back to client_id."""
        self.assertEqual(get_client_username("P645M2"), "Jagdeesh Sawant")
        self.assertEqual(get_client_username("NON_EXISTENT_ID"), "NON_EXISTENT_ID")
        self.assertEqual(get_client_username(""), "")

    @patch("utils.user_mapping.get_dynamic_username")
    def test_user_mapping_dynamic_priority(self, mock_dyn_username):
        """Verify dynamic credentials username takes priority over default static names."""
        mock_dyn_username.return_value = "Custom Dynamic User"
        self.assertEqual(get_client_username("FZ48062"), "Custom Dynamic User")

    async def test_health_monitor_healthy_with_valid_db(self):
        """Verify HealthMonitor._run_health_check marks system healthy when DB responds."""
        async with isolated_test_db():
            monitor = HealthMonitor()
            self.assertFalse(monitor.is_healthy)
            await monitor._run_health_check()
            self.assertTrue(monitor.is_healthy)
            self.assertIsNotNone(monitor.last_check)

    async def test_health_monitor_unhealthy_when_db_fails(self):
        """Verify HealthMonitor marks system unhealthy when DB query fails."""
        monitor = HealthMonitor()
        with patch("health.monitor.get_db") as mock_get_db:
            mock_get_db.side_effect = RuntimeError("Database unreachable")
            await monitor._run_health_check()
            self.assertFalse(monitor.is_healthy)


if __name__ == "__main__":
    unittest.main()
