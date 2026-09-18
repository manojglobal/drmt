import asyncio
import csv
import aiofiles

_csv_lock = asyncio.Lock()

async def read_csv_safe(path: str) -> list[dict]:
    """Reads a CSV file asynchronously using a shared lock to prevent concurrent reads."""
    async with _csv_lock:
        async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
            content = await f.read()
            
    # Parse the CSV string synchronously (CPU-bound, but generally fast enough for small CSVs)
    # If the CSV is huge, we should offload to a thread pool, but for our case this is fine.
    reader = csv.DictReader(content.splitlines())
    return [row for row in reader]
