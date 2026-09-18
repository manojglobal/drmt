import os
import csv
import json
import logging
from typing import Dict, List

logger = logging.getLogger("DRMT.UserMapping")

_cid_to_name: Dict[str, str] = {}
_name_to_cids: Dict[str, List[str]] = {}
_last_mtime: float = 0.0

def _get_csv_path() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "static", "plato_client_credentials.csv")

def _reload_if_needed() -> None:
    global _cid_to_name, _name_to_cids, _last_mtime
    csv_path = _get_csv_path()
    if not os.path.exists(csv_path):
        return

    try:
        mtime = os.path.getmtime(csv_path)
        if mtime == _last_mtime and _cid_to_name:
            return

        cid_map: Dict[str, str] = {}
        name_map: Dict[str, List[str]] = {}

        with open(csv_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("id") or not row.get("broker_creds"):
                    continue
                first = (row.get("first_name") or "").strip()
                last = (row.get("last_name") or "").strip()
                full_name = f"{first} {last}".strip() if last else first

                try:
                    creds_json = json.loads(row.get("broker_creds") or "{}")
                    cid = creds_json.get("client_id")
                    if cid and full_name:
                        cid_map[cid] = full_name
                        name_map.setdefault(full_name, []).append(cid)

                    uid = str(row.get("user_id") or "").strip()
                    if uid and full_name:
                        cid_map[uid] = full_name
                except Exception:
                    pass

        _cid_to_name = cid_map
        _name_to_cids = name_map
        _last_mtime = mtime
    except Exception as e:
        logger.error(f"Failed to load credentials CSV mapping: {e}")

def get_client_username(client_id: str) -> str:
    """Returns the full username for a given client_id, falling back to client_id if not mapped."""
    if not client_id:
        return ""
    _reload_if_needed()
    return _cid_to_name.get(str(client_id), str(client_id))

def get_client_ids_for_user(query_val: str) -> List[str]:
    """Returns all client_ids associated with a username or client_id."""
    if not query_val:
        return []
    _reload_if_needed()
    if query_val in _name_to_cids:
        return _name_to_cids[query_val]
    return [query_val]
