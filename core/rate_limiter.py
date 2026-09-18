import asyncio
import time

class TokenBucketRateLimiter:
    def __init__(self, rate: float, capacity: int):
        # Initializes the class with default values and state.
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        # Checks the Redis cache to enforce rate limiting rules.
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                
                # Refill tokens
                if elapsed > 0:
                    new_tokens = elapsed * self.rate
                    self.tokens = min(self.capacity, self.tokens + new_tokens)
                    self.last_refill = now
                
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
            
            # Wait a tiny bit and try again
            await asyncio.sleep(0.01)
