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
    # Access tokens and session tokens (quoted and unquoted)
    (re.compile(r'([\"\']?(?:access_token|auth_token|session_token)[\"\']?\s*[:=]\s*[\"\']?)([^\"\'\s,;}{]+)([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
    # General auth token: mask only if value contains letters & length >= 8 (avoids masking numerical instrument tokens like "token": "3787")
    (re.compile(r'([\"\']?token[\"\']?\s*[:=]\s*[\"\']?)(?=[^\"\'\s,;}{]*[a-zA-Z])([^\"\'\s,;}{]{8,})([\"\']?)', re.IGNORECASE), r'\g<1>***\g<3>'),
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


def clean_error_message(msg: str) -> str:
    """
    Cleans and standardizes error messages for console and error log files:
      1. Strips HTML error pages into clean single-line summaries.
      2. Removes internal proxy URL and debug artifacts (e.g. url=URL(...)).
      3. Normalizes aiohttp error formats (e.g. 500, message='...' -> HTTP 500 (...)).
      4. Cleans verbose Python dict syntax from broker responses.
      5. Deduplicates repeated error prefixes (e.g. 'Flattrade Auth Error: Flattrade Auth Error:').
      6. Flattens newlines and collapses whitespace to ensure strictly single-line log entries.
      Note: Full Response and Positions Payload messages are protected from dict stripping.
    """
    if not isinstance(msg, str):
        msg = str(msg)

    # 1. Flatten newlines and tabs
    msg = re.sub(r'[\r\n\t]+', ' ', msg)

    # 2. Strip HTML tags: <html>...<h1>502 Bad Gateway</h1>...</html>
    if '<html' in msg.lower() or '<body' in msg.lower() or '<h1>' in msg.lower():
        msg = re.sub(r'<[^>]+>', ' ', msg)
        msg = re.sub(r'\s+', ' ', msg).strip()

    # 3. Strip internal URL noise: url=URL('...') or url='...'
    msg = re.sub(r',?\s*url=(?:URL\()?[^)]*\)?', '', msg)

    # 4. Clean aiohttp client error format: 500, message='Internal Server Error'
    msg = re.sub(r'(\d{3}),\s*message=[\'\"]([^\'\"]*)[\'\"]', r'HTTP \1 (\2)', msg)

    # 5. Clean python dict-like broker errors: {'status': ..., 'message': '...', 'errorcode': '...'}
    # (Skip if the message is explicitly labeled with Full Response or Payload to preserve complete data)
    if "Full Response:" not in msg and "Positions Payload:" not in msg:
        def _clean_dict_match(m: re.Match) -> str:
            content = m.group(0)
            msg_match = re.search(r'[\'\"]message[\'\"]\s*:\s*[\'\"]([^\'\"]+)[\'\"]', content)
            err_match = re.search(r'[\'\"]errorcode[\'\"]\s*:\s*[\'\"]([^\'\"]+)[\'\"]', content)
            if msg_match:
                err_text = msg_match.group(1).strip()
                code_text = err_match.group(1).strip() if err_match else ''
                if code_text and code_text.lower() != 'none':
                    return f"{err_text} ({code_text})"
                return err_text
            return content

        msg = re.sub(r'\{[^{}]*[\'\"]message[\'\"][^{}]*\}', _clean_dict_match, msg)

    # 6. Deduplicate repeated prefixes (e.g. 'Flattrade Auth Error: Flattrade Auth Error:')
    msg = re.sub(r'(\b[A-Za-z0-9_\s]+ Error:)\s+\1', r'\1', msg)

    # 7. Strip trailing/redundant garbage like ', msg=None', ', data=None' (skip for full responses)
    if "Full Response:" not in msg and "Positions Payload:" not in msg:
        msg = re.sub(r',\s*(?:msg|data)=None', '', msg)
        msg = re.sub(r'[\'\"]data[\'\"]:\s*None,?\s*', '', msg)

    # 8. Collapse whitespace
    msg = re.sub(r'\s{2,}', ' ', msg).strip()
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
        if level in ("ERROR", "WARN"):
            msg = clean_error_message(msg)
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
        # Routine wire proxy traces and UI pings (api/audit) are kept in files to keep terminal clean
        if self.name not in ("api", "audit"):
            lvl_map = {"INFO": logging.INFO, "WARN": logging.WARNING, "ERROR": logging.ERROR}
            self._stdlib_logger.log(
                lvl_map.get(level, logging.INFO),
                msg,
                extra={"origin_module": module},
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


# ─── Standard DRMT Terminal Formatter ────────────────────────────────────────

class DRMTLogFormatter(logging.Formatter):
    """
    Standard DRMT log formatter matching reference terminal format:
        2026-09-28 09:31:24.479 | INFO     | main.py:70 | Starting DRMT V2...
    """
    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_IST)
        timestamp = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        lvl_name = "WARN" if record.levelname == "WARNING" else record.levelname
        level_str = f"{lvl_name:<8}"
        module = getattr(record, "origin_module", None)
        if not module:
            module = f"{record.filename}:{record.lineno}"
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            message = clean_error_message(message)
        return f"{timestamp} | {level_str} | {module} | {message}"


def setup_logging(level: int = logging.INFO) -> None:
    """Configures the root logger with the standard DRMT terminal formatter."""
    import sys
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(DRMTLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


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
    "DRMTLogFormatter",
    "setup_logging",
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
    "clean_error_message",
]
