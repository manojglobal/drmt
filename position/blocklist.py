import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional
import zoneinfo

logger = logging.getLogger("DRMT.Blocklist")

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")

_NON_AUTH_INFRA_PHRASES = (
    "http session not initialized",
    "session not initialized",
)

_AUTH_PHRASES = (
    "unauthorized",
    "unauthorised",
    "invalid token",
    "token invalid",
    "token expired",
    "token is expired",
    "token has expired",
    "expired token",
    "session expired",
    "session is expired",
    "session has expired",
    "invalid session",
    "session invalid",
    "session timed out",
    "session timeout",
    "session revoked",
    "session token",
    "invalid key",
    "invalid jwt",
    "jwt expired",
    "jwt token",
    "forbidden",
    "auth error",
    "auth failed",
    "auth rejected",
    "authentication failed",
    "broker auth fail",
    "not logged in",
    "user not logged in",
    "invalid user",
    "invalid client",
    "invalid password",
    "wrong password",
    "invalid totp",
    "invalid mpin",
    "access denied",
    "account locked",
    "user blocked",
    "re-authenticate",
    "authenticate",
    "missing access token",
    "missing session token",
    "no token available",
)

_AUTH_STATUS_CODE_RE = re.compile(r"(?<!\d)(?:401|403)(?!\d)")
_AUTH_BROKER_CODE_RE = re.compile(
    r"(?<!\d)(?:-17|-50|100022|100010|100008|100014|100015|100003|1037|1004|1081|1082|1090|1091|1010|1200)(?!\d)"
)
_TRANSIENT_HTTP_RE = re.compile(r"(?<!\d)(?:500|502|503|504|429)(?!\d)|\bia500\b", re.IGNORECASE)
_TRANSIENT_NET_PHRASES = (
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "connection reset",
    "connection refused",
    "internal server error",
    "timed out",
    "timeout",
    "http session not initialized",
    "session not initialized",
)


def is_auth_failure_message(err_msg: str) -> bool:
    """Returns True only when an error text indicates a real auth/session failure rather than infra/5xx errors."""
    low = str(err_msg or "").lower()
    if not low:
        return False
    if any(p in low for p in _NON_AUTH_INFRA_PHRASES):
        return False
    has_auth_code = bool(_AUTH_STATUS_CODE_RE.search(low) or _AUTH_BROKER_CODE_RE.search(low))
    if _TRANSIENT_HTTP_RE.search(low) and not has_auth_code:
        return False
    if has_auth_code:
        return True
    return any(p in low for p in _AUTH_PHRASES)


def is_transient_error(exc: BaseException) -> bool:
    """Returns True when an exception represents a transient network/gateway failure rather than auth rejection."""
    err_msg = str(exc or "")
    if is_auth_failure_message(err_msg):
        return False
    low = err_msg.lower()
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        return True
    if _TRANSIENT_HTTP_RE.search(low):
        return True
    return any(p in low for p in _TRANSIENT_NET_PHRASES)

# In-memory registry for blocked credentials: client_id -> dict
_blocked_users: Dict[str, dict] = {}
_transient_auth_failures: set[str] = set()


def mark_transient_auth_failure(client_id: str, is_transient: bool) -> None:
    """Tracks whether the latest refresh attempt for client_id failed transiently without auth rejection."""
    cid = str(client_id or "").strip()
    if not cid:
        return
    if is_transient:
        _transient_auth_failures.add(cid)
    else:
        _transient_auth_failures.discard(cid)


def consume_transient_auth_failure(client_id: str) -> bool:
    """Returns and clears whether client_id's latest auth failure was transient."""
    cid = str(client_id or "").strip()
    if cid in _transient_auth_failures:
        _transient_auth_failures.discard(cid)
        return True
    return False


def add_to_blocklist(client_id: str, broker: str, username: str, reason: str, signature: Optional[str] = None) -> None:
    """Adds or updates a credential in the Block List with failure details and credential signature."""
    _transient_auth_failures.discard(str(client_id or "").strip())
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
    _transient_auth_failures.discard(str(client_id or "").strip())
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
    _transient_auth_failures.clear()
    logger.info(f"🧹 [BLOCKLIST] Cleared all {count} entries")
    return count
