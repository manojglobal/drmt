"""
logger/logger.py — Five async service loggers for DRMT V2.

Provides a ServiceLogger class with:
- Async-safe in-memory history (newest first, max 100 entries)
- Non-blocking hourly file rotation via aiofiles
- Auto-fires send_error_email() via asyncio.create_task on every ERROR

Log format:
    2026-01-01 12:00:00 | INFO     | engine.py:42 | Message

Globals:
    logger_auth, logger_exit, logger_monitor, logger_system, logger_health
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import aiofiles
import aiofiles.os

_IST = ZoneInfo("Asia/Kolkata")
_stdlib = logging.getLogger(__name__)
_LOG_BASE_DIR = "data/logs"


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    id: int          # time.time_ns() — monotonic unique id
    timestamp: str   # IST datetime "YYYY-MM-DD HH:MM:SS"
    level: str       # INFO | WARN | ERROR
    message: str
    module: str      # "filename.py:lineno"


# ─── ServiceLogger ────────────────────────────────────────────────────────────

class ServiceLogger:
    """
    Async-safe structured logger for a named DRMT service.
    History is bounded to MAX_HISTORY, newest entry at index 0.
    All mutations are protected by asyncio.Lock.
    """

    MAX_HISTORY = 100

    def __init__(self, name: str) -> None:
        # Initializes the class with default values and state.
        self.name = name
        self._history: List[LogEntry] = []
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def info(self, msg: str) -> None:
        # Logs an informational message to the console and file.
        await self._log("INFO", msg)

    async def warn(self, msg: str) -> None:
        # Logs a warning message to the console and file.
        await self._log("WARN", msg)

    async def error(self, msg: str) -> None:
        """Log ERROR and fire a best-effort async email notification."""
        module = self._caller_module()
        await self._log("ERROR", msg, module_override=module)
        # Fire-and-forget: never blocks, never raises to caller
        try:
            from utils.email import send_error_email
            asyncio.create_task(
                send_error_email(f"DRMT ERROR [{self.name}]: {module}", msg),
                name=f"err_email_{self.name}",
            )
        except RuntimeError:
            pass  # No running event loop (e.g. during unit tests)

    def get_history(self) -> List[LogEntry]:
        """Return a shallow copy of the history list (newest first)."""
        return list(self._history)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _log(self, level: str, msg: str, module_override: Optional[str] = None) -> None:
        # Internal helper that formats and writes log entries.
        module = module_override or self._caller_module()
        now_ist = datetime.now(tz=_IST)
        timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        entry = LogEntry(
            id=time.time_ns(),
            timestamp=timestamp,
            level=level,
            message=msg,
            module=module,
        )

        async with self._lock:
            self._history.insert(0, entry)
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[:self.MAX_HISTORY]

        # Async file write — non-blocking, fire-and-forget
        asyncio.create_task(
            self._write_to_file(entry, now_ist),
            name=f"log_write_{self.name}",
        )

        # Mirror to stdlib logging for systemd/docker log capture
        lvl_map = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}
        _stdlib.log(
            lvl_map.get(level, logging.INFO),
            "[%s] %s | %s | %s", self.name, level, module, msg,
        )

    async def _write_to_file(self, entry: LogEntry, now_ist: datetime) -> None:
        """
        Append one log line to a rotating hourly file.
        INFO/WARN  → data/logs/<name>/app_YYYY-MM-DD_HH.log
        ERROR      → data/logs/<name>/error_YYYY-MM-DD_HH.log
        """
        try:
            date_str = now_ist.strftime("%Y-%m-%d_%H")
            prefix = "error" if entry.level == "ERROR" else "app"
            log_dir = os.path.join(_LOG_BASE_DIR, self.name)
            log_path = os.path.join(log_dir, f"{prefix}_{date_str}.log")

            await aiofiles.os.makedirs(log_dir, exist_ok=True)

            line = (
                f"{entry.timestamp} | {entry.level:<8} | "
                f"{entry.module} | {entry.message}\n"
            )
            async with aiofiles.open(log_path, mode="a", encoding="utf-8") as fh:
                await fh.write(line)
        except Exception:
            _stdlib.debug("ServiceLogger._write_to_file: suppressed exception", exc_info=True)

    @staticmethod
    def _caller_module() -> str:
        """Walk the call stack to find the first frame outside this file."""
        this_file = os.path.abspath(__file__)
        for frame_info in inspect.stack():
            frame_file = os.path.abspath(frame_info.filename)
            if frame_file != this_file:
                return f"{os.path.basename(frame_file)}:{frame_info.lineno}"
        return "unknown:0"


# ─── Module-level singletons ──────────────────────────────────────────────────

logger_auth:    ServiceLogger = ServiceLogger("auth")
logger_exit:    ServiceLogger = ServiceLogger("exit")
logger_monitor: ServiceLogger = ServiceLogger("monitor")
logger_system:  ServiceLogger = ServiceLogger("system")
logger_health:  ServiceLogger = ServiceLogger("health")

__all__ = [
    "LogEntry",
    "ServiceLogger",
    "logger_auth",
    "logger_exit",
    "logger_monitor",
    "logger_system",
    "logger_health",
]
