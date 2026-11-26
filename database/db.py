import asyncio
from contextlib import asynccontextmanager
import contextvars
import logging
import os

import aiosqlite

from settings import settings

logger = logging.getLogger("DRMT.DB")

# Tracks the asyncio.Task holding a connection so same-task re-entrant get_db() fails fast instead of deadlocking.
_active_db_task: contextvars.ContextVar[asyncio.Task | None] = contextvars.ContextVar("_active_db_task", default=None)

class DatabasePool:
    def __init__(self, db_path: str, max_connections: int = 5, acquire_timeout: float = 15.0):
        # Initializes the class with default values and state.
        self.db_path = db_path
        self.max_connections = max_connections
        self.acquire_timeout = acquire_timeout
        self._semaphore = asyncio.Semaphore(max_connections)
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._connections = []
        self._initialized = False

    async def init_db(self):
        """Run migrations and set up DB."""
        if self._initialized:
            return

        # Resolve absolute path relative to project root or app dir so CWD doesn't matter
        from utils.paths import get_bundle_dir, get_app_dir
        if not os.path.isabs(self.db_path):
            base_dir_root = get_app_dir()
            self.db_path = os.path.join(base_dir_root, self.db_path)

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            
            # Read migrations from bundled resource directory
            bundle_dir = get_bundle_dir()
            mig_001 = os.path.join(bundle_dir, "migrations", "001_init.sql")
            mig_002 = os.path.join(bundle_dir, "migrations", "002_credentials_extended.sql")

            if os.path.exists(mig_001):
                with open(mig_001, 'r') as f:
                    await conn.executescript(f.read())
            
            if os.path.exists(mig_002):
                with open(mig_002, 'r') as f:
                    statements = f.read().split(';')
                    for stmt in statements:
                        if not stmt.strip():
                            continue
                        try:
                            await conn.execute(stmt)
                        except aiosqlite.OperationalError as e:
                            if "duplicate column name" in str(e).lower():
                                logger.debug(f"Ignoring duplicate column error during migration: {e}")
                            else:
                                raise

            mig_003 = os.path.join(bundle_dir, "migrations", "003_token_columns.sql")
            if os.path.exists(mig_003):
                with open(mig_003, 'r') as f:
                    statements = f.read().split(';')
                    for stmt in statements:
                        if not stmt.strip():
                            continue
                        try:
                            await conn.execute(stmt)
                        except aiosqlite.OperationalError as e:
                            if "duplicate column name" in str(e).lower():
                                logger.debug(f"Ignoring duplicate column error during migration 003: {e}")
                            else:
                                raise

            mig_004 = os.path.join(bundle_dir, "migrations", "004_auth_audit_logs.sql")
            if os.path.exists(mig_004):
                with open(mig_004, 'r') as f:
                    statements = f.read().split(';')
                    for stmt in statements:
                        if not stmt.strip():
                            continue
                        try:
                            await conn.execute(stmt)
                        except aiosqlite.OperationalError as e:
                            logger.debug(f"Ignoring operational error during migration 004: {e}")

            mig_005 = os.path.join(bundle_dir, "migrations", "005_axis_kotak_credentials.sql")
            if os.path.exists(mig_005):
                with open(mig_005, 'r') as f:
                    statements = f.read().split(';')
                    for stmt in statements:
                        if not stmt.strip():
                            continue
                        try:
                            await conn.execute(stmt)
                        except aiosqlite.OperationalError as e:
                            if "duplicate column name" in str(e).lower():
                                logger.debug(f"Ignoring duplicate column error during migration 005: {e}")
                            else:
                                raise
            await conn.commit()
        
        self._initialized = True
        logger.info("🗄️  Database initialized successfully.")

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Returns a semaphore bound to the active running event loop."""
        loop = asyncio.get_running_loop()
        if self._semaphore_loop is not loop:
            # Lazily rebind semaphore when accessed from a new event loop (e.g. reloaders/tests).
            self._semaphore = asyncio.Semaphore(self.max_connections)
            self._semaphore_loop = loop
        return self._semaphore

    @asynccontextmanager
    async def get_connection(self):
        """Context manager yielding a DB connection restricted by semaphore."""
        current_task = asyncio.current_task()
        if current_task is not None and _active_db_task.get() is current_task:
            raise RuntimeError(
                "Reentrant get_db() call detected on the same task; exit outer get_db() before acquiring another."
            )

        sem = self._get_semaphore()
        acquired = False
        try:
            acquired = await asyncio.wait_for(sem.acquire(), timeout=self.acquire_timeout)
            token = _active_db_task.set(current_task)
            conn = None
            try:
                conn = await aiosqlite.connect(self.db_path)
                await conn.execute("PRAGMA foreign_keys=ON;")
                yield conn
            finally:
                _active_db_task.reset(token)
                if conn is not None:
                    await conn.close()
        finally:
            if acquired:
                sem.release()

db_pool = DatabasePool(settings.DB_PATH, settings.DB_POOL_SIZE)

async def init_db():
    # Runs the database migrations and prepares tables.
    await db_pool.init_db()

@asynccontextmanager
async def get_db():
    # Retrieves a safe connection to the SQLite database.
    async with db_pool.get_connection() as conn:
        yield conn
