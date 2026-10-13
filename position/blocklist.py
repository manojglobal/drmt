import logging
from datetime import datetime
from typing import Dict, List, Optional
import zoneinfo

logger = logging.getLogger("DRMT.Blocklist")

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# In-memory registry for blocked credentials: client_id -> dict
_blocked_users: Dict[str, dict] = {}


def add_to_blocklist(client_id: str, broker: str, username: str, reason: str, signature: Optional[str] = None) -> None:
    """Adds or updates a credential in the Block List with failure details and credential signature."""
    now_ist = datetime.now(tz=_IST)
    timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    prev_sig = _blocked_users.get(client_id, {}).get("credential_signature", "")
    _blocked_users[client_id] = {
        "client_id": client_id,
        "broker": broker,
        "username": username or client_id,
        "reason": str(reason),
        "blocked_at": timestamp,
        "status": "blocked",
        "credential_signature": signature or prev_sig,
    }
    logger.warning(
        f"⛔ [BLOCKLIST] Added {broker}:{client_id} ({username}) - Reason: {reason}"
    )


def get_blocked_user_signature(client_id: str) -> Optional[str]:
    """Returns the credential signature for a blocked user."""
    return _blocked_users.get(client_id, {}).get("credential_signature")


def update_blocked_user_signature(client_id: str, signature: str, reason: Optional[str] = None) -> None:
    """Updates the credential signature and reason for an existing blocked user."""
    if client_id in _blocked_users:
        _blocked_users[client_id]["credential_signature"] = signature
        if reason:
            _blocked_users[client_id]["reason"] = str(reason)


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
    """Returns a list of all currently blocked credentials (newest first), ensuring username is resolved."""
    from utils.user_mapping import get_client_username
    res = []
    for item in sorted(list(_blocked_users.values()), key=lambda x: x.get("blocked_at", ""), reverse=True):
        entry = dict(item)
        cid = str(entry.get("client_id") or "")
        current_user = entry.get("username") or ""
        if not current_user or current_user == cid:
            resolved = get_client_username(cid)
            if resolved:
                entry["username"] = resolved
        res.append(entry)
    return res


def get_blocked_user(client_id: str) -> Optional[dict]:
    """Returns details for a specific blocked credential if present, ensuring username is resolved."""
    entry = _blocked_users.get(client_id)
    if not entry:
        return None
    res = dict(entry)
    cid = str(res.get("client_id") or "")
    current_user = res.get("username") or ""
    if not current_user or current_user == cid:
        from utils.user_mapping import get_client_username
        resolved = get_client_username(cid)
        if resolved:
            res["username"] = resolved
    return res


def clear_blocklist() -> int:
    """Clears all entries from the Block List (primarily for testing/admin reset / morning daily reset)."""
    count = len(_blocked_users)
    _blocked_users.clear()
    logger.info(f"🧹 [BLOCKLIST] Cleared all {count} entries")
    return count
