"""
utils/user_mapping.py — User and Client ID Mapping.
Provides user display names and client ID mapping sourced dynamically from the remote feed.
"""

import logging
from typing import Dict, List, Set

from credentials.dynamic_loader import (
    get_dynamic_configured_cids,
    get_dynamic_username,
    get_dynamic_cids_for_user,
)

logger = logging.getLogger("DRMT.UserMapping")

DEFAULT_CLIENT_NAMES: Dict[str, str] = {
    "P645M1": "Manoj mStock Demo",
    "P645M2": "Jagdeesh Sawant",
    "P645M6": "Rinku Singh",
    "FZ48062": "Rinku Singh",
    "2812175": "Angand Singh",
    "1905469": "Dilip Kushwah",
    "1923416": "Rakesh Alice Demo",
    "807663": "Manoj Singh",
}

_fallback_configured_cids: Set[str] = set(DEFAULT_CLIENT_NAMES.keys())


def get_configured_client_ids() -> Set[str]:
    """Returns the set of active client IDs configured via dynamic remote feed or database."""
    dynamic_cids = get_dynamic_configured_cids()
    if dynamic_cids:
        return dynamic_cids
    return set(_fallback_configured_cids)


def get_client_username(client_id: str) -> str:
    """Returns the full display name for a given client_id, falling back to client_id if not mapped."""
    if not client_id:
        return ""
    dyn_name = get_dynamic_username(client_id)
    if dyn_name and dyn_name != client_id:
        return dyn_name
    return DEFAULT_CLIENT_NAMES.get(str(client_id), str(client_id))


def get_client_ids_for_user(query_val: str) -> List[str]:
    """Returns all client_ids associated with a username or client_id."""
    if not query_val:
        return []
    dyn_cids = get_dynamic_cids_for_user(query_val)
    if dyn_cids and dyn_cids != [query_val]:
        return dyn_cids
    matched = [cid for cid, name in DEFAULT_CLIENT_NAMES.items() if name == query_val]
    return matched if matched else [query_val]
