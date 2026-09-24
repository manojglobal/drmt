import logging
from datetime import datetime
from typing import Dict, List, Optional
import zoneinfo

logger = logging.getLogger("DRMT.Blocklist")

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# In-memory registry for blocked credentials: client_id -> dict
_blocked_users: Dict[str, dict] = {}


def add_to_blocklist(client_id: str, broker: str, username: str, reason: str) -> None:
    """Adds or updates a credential in the Block List with failure details."""
    now_ist = datetime.now(tz=_IST)
    timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    _blocked_users[client_id] = {
        "client_id": client_id,
        "broker": broker,
        "username": username or client_id,
        "reason": str(reason),
        "blocked_at": timestamp,
        "status": "blocked",
    }
    logger.warning(
        f"⛔ [BLOCKLIST] Added {broker}:{client_id} ({username}) - Reason: {reason}"
    )


def remove_from_blocklist(client_id: str) -> bool:
    """Removes a credential from the Block List upon successful authentication."""
    if client_id in _blocked_users:
        removed = _blocked_users.pop(client_id)
        logger.info(
            f"✅ [BLOCKLIST] Removed {removed.get('broker')}:{client_id} ({removed.get('username')}) from Block List"
        )
        return True
    return False


def is_blocked(client_id: str) -> bool:
    """Checks whether a client_id is currently blocked."""
    return client_id in _blocked_users


def get_blocked_users() -> List[dict]:
    """Returns a list of all currently blocked credentials (newest first)."""
    return sorted(
        list(_blocked_users.values()),
        key=lambda x: x.get("blocked_at", ""),
        reverse=True,
    )


def get_blocked_user(client_id: str) -> Optional[dict]:
    """Returns details for a specific blocked credential if present."""
    return _blocked_users.get(client_id)


def clear_blocklist() -> None:
    """Clears all entries from the Block List (primarily for testing/admin reset)."""
    _blocked_users.clear()
    logger.info("🧹 [BLOCKLIST] Cleared all entries")
