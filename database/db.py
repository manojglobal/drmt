import asyncio
import logging
from contextlib import asynccontextmanager
import aiosqlite
import os

from settings import settings

logger = logging.getLogger("DRMT.DB")

class DatabasePool:
    def __init__(self, db_path: str, max_connections: int = 5):
        # Initializes the class with default values and state.
        self.db_path = db_path
        self._semaphore = asyncio.Semaphore(max_connections)
        self._connections = []
        self._initialized = False

    async def init_db(self):
        """Run migrations and set up DB."""
        if self._initialized:
            return

        # Resolve absolute path relative to project root so CWD doesn't matter
        if not os.path.isabs(self.db_path):
            base_dir_root = os.path.dirname(os.path.dirname(__file__))
            self.db_path = os.path.join(base_dir_root, self.db_path)

        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            
            # Read migrations
            base_dir = os.path.dirname(os.path.dirname(__file__))
            mig_001 = os.path.join(base_dir, "migrations", "001_init.sql")
            mig_002 = os.path.join(base_dir, "migrations", "002_credentials_extended.sql")

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

            mig_003 = os.path.join(base_dir, "migrations", "003_token_columns.sql")
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
            await conn.commit()
        
        self._initialized = True
        logger.info("🗄️  Database initialized successfully.")

    @asynccontextmanager
    async def get_connection(self):
        """Context manager yielding a DB connection restricted by semaphore."""
        async with self._semaphore:
            conn = await aiosqlite.connect(self.db_path)
            try:
                await conn.execute("PRAGMA foreign_keys=ON;")
                yield conn
            finally:
                await conn.close()

db_pool = DatabasePool(settings.DB_PATH, settings.DB_POOL_SIZE)

async def init_db():
    # Runs the database migrations and prepares tables.
    await db_pool.init_db()

@asynccontextmanager
async def get_db():
    # Retrieves a safe connection to the SQLite database.
    async with db_pool.get_connection() as conn:
        yield conn
