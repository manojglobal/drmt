"""
logger/logger.py — Five async service loggers for DRMT V2.

Provides a ServiceLogger class with:
- Async-safe in-memory history (newest first, max 100 entries)
- Non-blocking daily file rotation via aiofiles with 30-day retention
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
from datetime import datetime, timedelta
import re
from typing import List, Optional
from zoneinfo import ZoneInfo

import aiofiles
import aiofiles.os

from utils.email import send_error_email
from utils.paths import get_app_dir

_IST = ZoneInfo("Asia/Kolkata")
_stdlib = logging.getLogger(__name__)
_LOG_BASE_DIR = os.path.join(get_app_dir(), "data", "logs")


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class LogEntry:
    id: int          # time.time_ns() — monotonic unique id
    timestamp: str   # IST datetime "YYYY-MM-DD HH:MM:SS"
    level: str       # INFO | WARN | ERROR
    message: str
    module: str      # "filename.py:lineno"


# ─── Credential Sanitization (Zero-Leakage Guarantee) ─────────────────────────

SENSITIVE_PATTERNS = [
    # Passwords in JSON, query strings, or logs (quoted and unquoted)
    (re.compile(r'([\"\']?password[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
    # TOTP secrets
    (re.compile(r'([\"\']?totp_secret(?:_key)?[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
    # API keys & private keys
    (re.compile(r'([\"\']?(?:api_key|api_secret|app_code|X-PrivateKey|private_key)[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
    # Access tokens and tokens
    (re.compile(r'([\"\']?(?:access_token|token)[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
    # Bearer tokens: mask inner characters
    (re.compile(r'(Bearer\s+)([A-Za-z0-9_\-\.]{6})([A-Za-z0-9_\-\.]+)([A-Za-z0-9_\-\.]{4})', re.IGNORECASE), r'\g<1>\g<2>***\g<4>'),
    # JWT tokens in general
    (re.compile(r'(eyJ[A-Za-z0-9_\-]{6})([A-Za-z0-9_\-\.]+)([A-Za-z0-9_\-]{4})'), r'\g<1>***\g<3>'),
    # Flattrade jKey/susertoken
    (re.compile(r'([\"\']?jKey[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
    (re.compile(r'([\"\']?susertoken[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
]

def sanitize_log_message(msg: str) -> str:
    """Masks sensitive credentials (passwords, tokens, TOTP secrets, API keys) from log strings."""
    if not isinstance(msg, str):
        msg = str(msg)
    for pattern, repl in SENSITIVE_PATTERNS:
        msg = pattern.sub(repl, msg)
    return msg


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
        tag = "DRMT.SCM" if name in ("system", "monitor", "chronos") else f"DRMT.{name.capitalize()}"
        self._stdlib_logger = logging.getLogger(tag)

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
        msg = sanitize_log_message(msg)
        module = module_override or self._caller_module()
        now_ist = datetime.now(tz=_IST)
        timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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

        # Mirror to stdlib logging for clean terminal output matching reference format
        lvl_map = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}
        self._stdlib_logger.log(
            lvl_map.get(level, logging.INFO),
            msg,
        )

    async def _write_to_file(self, entry: LogEntry, now_ist: datetime) -> None:
        """
        Append one log line to a rotating daily file.
        INFO/WARN  → data/logs/<name>/app_YYYY-MM-DD.log
        ERROR      → data/logs/<name>/error_YYYY-MM-DD.log
        """
        try:
            date_str = now_ist.strftime("%Y-%m-%d")
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


# ─── 30-Day Retention & Automatic Cleanup ────────────────────────────────────

async def cleanup_old_logs(retention_days: int = 30) -> int:
    """
    Scans data/logs/<service>/ and removes any .log file older than retention_days.
    Dual-check validation:
      1. Parses YYYY-MM-DD from the filename (e.g. app_2026-08-15.log).
      2. Falls back to file modification timestamp (mtime).
    Returns the count of deleted files.
    """
    deleted_count = 0
    now_ist = datetime.now(tz=_IST)
    cutoff_date = (now_ist - timedelta(days=retention_days)).date()
    cutoff_ts = time.time() - (retention_days * 86400)

    if not os.path.exists(_LOG_BASE_DIR):
        return 0

    for root, _, files in os.walk(_LOG_BASE_DIR):
        for f in files:
            if not f.endswith(".log"):
                continue

            filepath = os.path.join(root, f)
            should_delete = False

            # Check 1: Parse date from filename (matches YYYY-MM-DD)
            match = re.search(r'(\d{4}-\d{2}-\d{2})', f)
            if match:
                try:
                    file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                    if file_date < cutoff_date:
                        should_delete = True
                except ValueError:
                    pass

            # Check 2: Fallback to file modification timestamp (mtime)
            if not should_delete:
                try:
                    mtime = os.path.getmtime(filepath)
                    if mtime < cutoff_ts:
                        should_delete = True
                except OSError:
                    pass

            if should_delete:
                try:
                    await aiofiles.os.remove(filepath)
                    deleted_count += 1
                except Exception:
                    pass

    if deleted_count > 0:
        _stdlib.info(f"Cleaned up {deleted_count} log file(s) older than {retention_days} days.")
    return deleted_count


async def start_log_cleanup_loop(retention_days: int = 30, interval_hours: int = 24) -> None:
    """Background task: runs cleanup once on boot, then repeats every interval_hours (default 24h)."""
    while True:
        try:
            await cleanup_old_logs(retention_days)
        except Exception as e:
            _stdlib.error(f"Error during log retention cleanup: {e}")
        await asyncio.sleep(interval_hours * 3600)


# ─── Module-level singletons ──────────────────────────────────────────────────

logger_auth:    ServiceLogger = ServiceLogger("auth")
logger_audit:   ServiceLogger = ServiceLogger("audit")
logger_api:     ServiceLogger = ServiceLogger("api")
logger_exit:    ServiceLogger = ServiceLogger("exit")
logger_monitor: ServiceLogger = ServiceLogger("monitor")
logger_system:  ServiceLogger = ServiceLogger("system")
logger_health:  ServiceLogger = ServiceLogger("health")

__all__ = [
    "LogEntry",
    "ServiceLogger",
    "logger_auth",
    "logger_audit",
    "logger_api",
    "logger_exit",
    "logger_monitor",
    "logger_system",
    "logger_health",
    "cleanup_old_logs",
    "start_log_cleanup_loop",
    "sanitize_log_message",
]
