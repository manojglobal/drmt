"""
utils/reloader.py — Automatic Code Reloader & Process Supervisor for DRMT V2.

Monitors project Python files (.py) and .env for modifications.
Automatically re-launches the application when code changes are saved,
enabling continuous live development without manual server restarts.
"""

import os
import sys
import time
import signal
import subprocess
from datetime import datetime
from typing import Dict, Optional, Set

from utils.paths import get_app_dir


def _log(level: str, msg: str) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now_str} | {level:<8} | reloader.py:1 | {msg}", flush=True)


class FileWatcher:
    """Lightweight file system watcher based on file modification times (mtime)."""

    def __init__(self, watch_dir: str):
        self.watch_dir = os.path.abspath(watch_dir)
        self.mtimes: Dict[str, float] = {}
        self.ignored_dirs: Set[str] = {
            ".git",
            "data",
            "logs",
            "__pycache__",
            "venv",
            ".venv",
            "build",
            "dist",
            ".claude",
            ".gemini",
            ".gitnexus",
            "node_modules",
        }
        self.watch_extensions: Set[str] = {".py"}
        self.watch_exact_files: Set[str] = {".env"}
        self.scan_all()

    def _should_skip_dir(self, dir_name: str) -> bool:
        return dir_name in self.ignored_dirs or dir_name.startswith(".")

    def scan_all(self) -> Dict[str, float]:
        """Scans the directory tree and records current mtimes for watched files."""
        snapshot: Dict[str, float] = {}
        for root, dirs, files in os.walk(self.watch_dir):
            # Prune ignored directories in-place for high performance
            dirs[:] = [d for d in dirs if not self._should_skip_dir(d)]
            for file_name in files:
                ext = os.path.splitext(file_name)[1]
                if ext in self.watch_extensions or file_name in self.watch_exact_files:
                    full_path = os.path.join(root, file_name)
                    try:
                        snapshot[full_path] = os.stat(full_path).st_mtime
                    except OSError:
                        pass
        self.mtimes = snapshot
        return snapshot

    def check_for_changes(self) -> Optional[str]:
        """
        Checks whether any watched file has been modified or newly added.
        Returns the relative path of the changed file, or None if unchanged.
        """
        for root, dirs, files in os.walk(self.watch_dir):
            dirs[:] = [d for d in dirs if not self._should_skip_dir(d)]
            for file_name in files:
                ext = os.path.splitext(file_name)[1]
                if ext in self.watch_extensions or file_name in self.watch_exact_files:
                    full_path = os.path.join(root, file_name)
                    try:
                        mtime = os.stat(full_path).st_mtime
                        if full_path not in self.mtimes:
                            self.mtimes[full_path] = mtime
                            return os.path.relpath(full_path, self.watch_dir)
                        elif mtime > self.mtimes[full_path]:
                            self.mtimes[full_path] = mtime
                            return os.path.relpath(full_path, self.watch_dir)
                    except OSError:
                        pass
        return None


def _terminate_process(proc: subprocess.Popen, timeout: float = 4.0) -> None:
    """Gracefully terminates a subprocess using SIGINT, escalating to SIGKILL if needed."""
    if proc.poll() is not None:
        return

    try:
        # First send SIGINT so aiohttp runs app.on_cleanup and frees sockets cleanly
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass

        # Escalate to SIGTERM
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass

        # Force kill if process is still alive
        proc.kill()
        proc.wait(timeout=1.0)
    except Exception:
        pass


def is_reloader_active() -> bool:
    """
    Determines if supervisor mode should be activated for the current process.
    Returns False if running frozen binary, if already a worker, or if AUTO_RELOAD is disabled.
    """
    if getattr(sys, "frozen", False):
        return False

    if os.environ.get("_DRMT_RELOADER_CHILD") == "1":
        return False

    try:
        from settings import settings
        return bool(getattr(settings, "AUTO_RELOAD", True))
    except Exception:
        return True


def run_supervisor() -> None:
    """
    Supervisor process loop. Starts the server worker subprocess and monitors project files.
    Restarts the worker automatically whenever code changes are saved.
    """
    app_dir = get_app_dir()
    watcher = FileWatcher(watch_dir=app_dir)

    _log("INFO", "👀 [AUTO-RELOAD] Supervisor active. Watching project files for changes...")

    shutting_down = False

    def handle_signal(signum, frame):
        nonlocal shutting_down
        shutting_down = True

    # Register signal handlers for clean terminal exit
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while not shutting_down:
        env = os.environ.copy()
        env["_DRMT_RELOADER_CHILD"] = "1"

        try:
            child = subprocess.Popen([sys.executable] + sys.argv, env=env)
        except Exception as e:
            _log("ERROR", f"❌ [AUTO-RELOAD] Failed to spawn worker process: {e}")
            time.sleep(2.0)
            continue

        restart_requested = False
        changed_file = None

        # Loop while child is running
        while not shutting_down:
            exit_code = child.poll()
            if exit_code is not None:
                # Child exited on its own
                if shutting_down:
                    break

                if exit_code != 0:
                    _log("WARNING", f"⚠️  [AUTO-RELOAD] Server process exited with code {exit_code}.")
                    _log("INFO", "⏳ [AUTO-RELOAD] Waiting for code changes before restarting...")
                else:
                    _log("INFO", "ℹ️  [AUTO-RELOAD] Server process exited cleanly.")
                    _log("INFO", "⏳ [AUTO-RELOAD] Waiting for code changes before restarting...")

                # Wait until user modifies a file or exits
                while not shutting_down:
                    changed = watcher.check_for_changes()
                    if changed:
                        changed_file = changed
                        restart_requested = True
                        break
                    time.sleep(0.8)
                break

            # Check for file changes while child is actively running
            changed = watcher.check_for_changes()
            if changed:
                changed_file = changed
                restart_requested = True
                break

            time.sleep(0.8)

        if restart_requested and not shutting_down:
            _log("INFO", f"🔄 [AUTO-RELOAD] Detected change in '{changed_file}'. Restarting DRMT server...")
            _terminate_process(child)
            # Debounce pause: allow disk writes to settle and snapshot mtimes
            time.sleep(0.5)
            watcher.scan_all()
        else:
            _terminate_process(child)
            break

    _log("INFO", "🛑 [AUTO-RELOAD] Supervisor stopped.")
