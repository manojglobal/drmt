import asyncio
import logging
from typing import Dict
import aiohttp
import redis.asyncio as redis

from settings import settings
from core.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger("DRMT.Engine")

class CoreEngine:
    def __init__(self):
        # Initializes the class with default values and state.
        self._worker_queue = None
        self._user_tasks: Dict[str, asyncio.Task] = {}
        
        self._api_sem = None
        self._exit_sem = None
        
        self._global_rate_limiter = None
        self._user_rate_limiters: Dict[str, TokenBucketRateLimiter] = {}
        self.last_ping: Dict[str, float] = {}
        
        self._workers = []
        self.http_session = None
        self.redis_pool = None

    async def start(self):
        # Initialize asyncio primitives inside the running event loop
        self._worker_queue = asyncio.Queue(maxsize=500)
        self._api_sem = asyncio.Semaphore(settings.API_CONCURRENCY)
        self._exit_sem = asyncio.Semaphore(20)
        self._global_rate_limiter = TokenBucketRateLimiter(settings.GLOBAL_RATE_LIMIT, settings.GLOBAL_RATE_LIMIT)
        
        # Start worker pool
        # Starts the main processes or background workers.
        for i in range(settings.WORKER_POOL_SIZE):
            task = asyncio.create_task(self._worker_loop(i))
            self._workers.append(task)
            
        # Global HTTP Session
        connector = aiohttp.TCPConnector(limit=50, limit_per_host=10)
        self.http_session = aiohttp.ClientSession(connector=connector)
        
        # Redis Pool
        self.redis_pool = redis.from_url(settings.REDIS_URL, decode_responses=True)
        
        logger.info("⚙️  Core Engine started.")

    async def _worker_loop(self, worker_id: int):
        # Continuously processes tasks from the background queue.
        while True:
            try:
                coro, future = await self._worker_queue.get()
                try:
                    result = await coro
                    if future and not future.done():
                        future.set_result(result)
                except Exception as e:
                    if future and not future.done():
                        future.set_exception(e)
                    logger.error(f"Worker {worker_id} error: {e}", exc_info=True)
                finally:
                    self._worker_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} critical error: {e}")

    async def async_submit(self, coro):
        # Adds a new task to the queue asynchronously, natively respecting backpressure (maxsize=500).
        future = asyncio.Future()
        await self._worker_queue.put((coro, future))
        return future
    async def spawn_user(self, user_id: str, ws, credentials):
        # Unique User Management: Cancel existing task if any
        # Creates a new background polling session for a user.
        if user_id in self._user_tasks and not self._user_tasks[user_id].done():
            logger.info(f"🔄 Cancelling existing task for user {user_id}")
            self._user_tasks[user_id].cancel()
            try:
                await self._user_tasks[user_id]
            except asyncio.CancelledError:
                pass
                
        # We will import _polling_loop dynamically to avoid circular import if needed
        from position.chronos import polling_loop
        
        task = asyncio.create_task(polling_loop(user_id, ws, credentials, self))
        self._user_tasks[user_id] = task
        logger.info(f"🚀 Spawned polling task for user_id {user_id} ({len(credentials)} credentials loaded)")

    def remove_user(self, user_id: str):
        # Stops and removes an active polling session for a user.
        if user_id in self._user_tasks:
            task = self._user_tasks.pop(user_id)
            if not task.done():
                task.cancel()

    def acquire_api_semaphore(self):
        # Safely acquires a lock before hitting external APIs.
        if self._api_sem is None:
            self._api_sem = asyncio.Semaphore(settings.API_CONCURRENCY)
        return self._api_sem

    def acquire_exit_semaphore(self):
        # Safely acquires a lock before executing a trade exit.
        if self._exit_sem is None:
            self._exit_sem = asyncio.Semaphore(20)
        return self._exit_sem

    async def acquire_rate_limit(self, user_id: str):
        # Per-user limit
        if user_id not in self._user_rate_limiters:
            self._user_rate_limiters[user_id] = TokenBucketRateLimiter(
                settings.USER_RATE_LIMIT, settings.USER_RATE_LIMIT
            )
        await self._user_rate_limiters[user_id].acquire()

    async def stop(self):
        # Safely shuts down all running workers and connections.
        logger.info("Stopping Core Engine...")
        # Cancel users
        for uid, task in self._user_tasks.items():
            if not task.done():
                task.cancel()
        
        # Cancel workers
        for task in self._workers:
            if not task.done():
                task.cancel()
                
        if self.http_session:
            await self.http_session.close()
            
        if self.redis_pool:
            try:
                await self.redis_pool.flushdb()
            except Exception as e:
                logger.error(f"Error flushing Redis on shutdown: {e}")
            await self.redis_pool.close()

core_engine = CoreEngine()
