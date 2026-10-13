"""
credentials/dynamic_loader.py — Dynamic Credential Fetcher & Database Sync.
Downloads client credentials directly from a remote URL (e.g. Dropbox CSV),
parses broker credentials, populates in-memory caches, and synchronizes
with the SQLite database.
"""

import csv
import hashlib
import io
import json
import logging
import os
from typing import Dict, List, Optional, Set

import aiohttp

from database.db import get_db
from logger.logger import logger_auth, logger_system, sanitize_log_message
from settings import settings

logger = logging.getLogger("DRMT.CredentialsSync")

# In-memory caches populated dynamically
_configured_cids: Set[str] = set()
_cid_to_name: Dict[str, str] = {}
_cid_to_email: Dict[str, str] = {}
_name_to_cids: Dict[str, List[str]] = {}
_client_creds: Dict[str, dict] = {}
_initialized: bool = False


def derive_user_email(first_name: str, client_id: str) -> str:
    """Derives user email matching historical Plato format (e.g. shivram@gmail.com, recover@gmail.com)."""
    fn = (first_name or "").strip().lower()
    cid = (client_id or "").strip().upper()
    if "recover" in fn or cid == "P645M6":
        return "recover@gmail.com"
    if fn:
        clean = "".join(ch for ch in fn.split()[0] if ch.isalnum())
        return f"{clean}@gmail.com"
    return f"{cid.lower()}@gmail.com"


def _normalize_dropbox_url(url: str) -> str:
    """Ensures Dropbox URLs download the raw CSV content directly."""
    if not url:
        return ""
    if "dropbox.com" in url:
        if "dl=0" in url:
            return url.replace("dl=0", "dl=1")
        if "dl=1" not in url:
            separator = "&" if "?" in url else "?"
            return f"{url}{separator}dl=1"
    return url


async def fetch_and_sync_credentials(url: Optional[str] = None) -> int:
    """
    Fetches credentials from local static CSV or remote URL, updates in-memory mapping,
    and UPSERTs active accounts into the SQLite credentials table.
    """
    global _configured_cids, _cid_to_name, _cid_to_email, _name_to_cids, _client_creds, _initialized

    from utils.paths import get_app_dir

    raw_csv_text = ""
    local_csv_path = None
    use_dropbox = getattr(settings, "USE_DROPBOX_CREDENTIALS", False)

    # ── BRANCH 1: DROPBOX CREDENTIALS (when USE_DROPBOX_CREDENTIALS is True) ──
    if use_dropbox:
        target_url = _normalize_dropbox_url(
            url or getattr(settings, "DROPBOX_CREDENTIALS_URL", "") or getattr(settings, "CREDENTIALS_CSV_URL", "")
        )
        if target_url:
            logger.info(f"🌐 [Credentials Sync] Source: DROPBOX (URL: {target_url})")
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(target_url, allow_redirects=True) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status} while fetching Dropbox credentials CSV")
                        raw_csv_text = await resp.text(encoding="utf-8")
                logger.info(f"✅ Successfully downloaded credentials CSV from Dropbox ({len(raw_csv_text)} bytes)")
                try:
                    static_cache_path = getattr(settings, "CREDENTIALS_CSV_PATH", "static/plato_client_credentials.csv")
                    if not os.path.isabs(static_cache_path):
                        static_cache_path = os.path.join(get_app_dir(), static_cache_path)
                    os.makedirs(os.path.dirname(static_cache_path), exist_ok=True)
                    with open(static_cache_path, "w", encoding="utf-8") as f_cache:
                        f_cache.write(raw_csv_text)
                except Exception as cache_err:
                    logger.debug(f"Could not cache Dropbox CSV to local file: {cache_err}")
            except Exception as fetch_err:
                logger.error(f"❌ Failed to fetch credentials from Dropbox URL: {fetch_err}. Falling back to local static CSV.")
                await logger_auth.warn(f"Failed to fetch credentials from Dropbox: {fetch_err}. Falling back to local static CSV.")
                raw_csv_text = ""
        else:
            logger.warning("USE_DROPBOX_CREDENTIALS is True, but no DROPBOX_CREDENTIALS_URL configured. Falling back to local CSV.")

    # ── BRANCH 2: LOCAL CSV CREDENTIALS (when USE_DROPBOX_CREDENTIALS is False or Dropbox fetch failed) ──
    if not raw_csv_text:
        configured_path = getattr(settings, "CREDENTIALS_CSV_PATH", "static/plato_client_credentials.csv")
        if configured_path:
            candidate_paths = [
                configured_path if os.path.isabs(configured_path) else os.path.join(get_app_dir(), configured_path),
                os.path.join(os.getcwd(), configured_path),
                os.path.join(get_app_dir(), "static", "plato_client_credentials.csv")
            ]
            for p in candidate_paths:
                if os.path.isfile(p):
                    local_csv_path = p
                    break

        if local_csv_path:
            logger.info(f"📄 [Credentials Sync] Source: LOCAL CSV ({local_csv_path})")
            try:
                with open(local_csv_path, "r", encoding="utf-8") as f:
                    raw_csv_text = f.read()
            except Exception as file_err:
                logger.error(f"❌ Failed to read local static credentials CSV: {file_err}")
                await logger_auth.error(f"Failed to read local static credentials CSV: {file_err}. Falling back to SQLite cache.")
                await _fallback_load_from_db()
                return len(_configured_cids)
        else:
            logger.warning("No credentials CSV file found. Falling back to SQLite cache.")
            await _fallback_load_from_db()
            return len(_configured_cids)

    if not raw_csv_text or not raw_csv_text.strip():
        logger.warning("Credentials CSV is empty. Falling back to SQLite cache.")
        await _fallback_load_from_db()
        return len(_configured_cids)

    # Parse CSV in memory
    reader = list(csv.DictReader(io.StringIO(raw_csv_text)))
    # Sort by numeric ID ascending so newer entries naturally evaluate later
    reader.sort(key=lambda r: int(r.get("id") or 0))

    new_cid_to_name: Dict[str, str] = {}
    new_cid_to_email: Dict[str, str] = {}
    new_name_to_cids: Dict[str, List[str]] = {}
    new_configured_cids: Set[str] = set()
    new_client_creds: Dict[str, dict] = {}
    synced_records: List[dict] = []

    for row in reader:
        if not row.get("id") or not row.get("broker_creds"):
            continue

        first = (row.get("first_name") or "").strip()
        last = (row.get("last_name") or "").strip()
        full_name = f"{first} {last}".strip() if last else first
        broker = str(row.get("broker") or "").strip().upper()
        static_ip = str(row.get("static_ip") or "").strip()

        try:
            creds_json = json.loads(row.get("broker_creds") or "{}")
        except Exception:
            continue

        client_id = creds_json.get("client_id")
        if not client_id:
            continue

        client_id = str(client_id).strip()
        new_configured_cids.add(client_id)

        token_created = creds_json.get("access_token_created_at") or ""
        token_val = creds_json.get("access_token") or creds_json.get("token") or ""
        row_id = int(row.get("id") or 0)

        # Name deduplication: prefer human names over generic labels (e.g. Demat, Virtual, Recovery)
        # When comparing two human names, prefer the newer record based on access_token_created_at or row_id
        is_generic_name = any(x in full_name.lower() for x in ("demat", "virtual", "recovery"))
        existing_name = new_cid_to_name.get(client_id, "")
        existing_is_generic = any(x in existing_name.lower() for x in ("demat", "virtual", "recovery"))

        should_update_name = False
        if not existing_name:
            should_update_name = True
        elif existing_is_generic and not is_generic_name:
            should_update_name = True
        elif not is_generic_name and not existing_is_generic:
            existing_cred = new_client_creds.get(client_id)
            if existing_cred:
                existing_time = existing_cred.get("token_created_at") or ""
                existing_row_id = existing_cred.get("row_id") or 0
                if token_created and existing_time:
                    if token_created > existing_time:
                        should_update_name = True
                    elif token_created == existing_time and row_id >= existing_row_id:
                        should_update_name = True
                elif row_id >= existing_row_id:
                    should_update_name = True
            else:
                should_update_name = True

        if should_update_name:
            if existing_name and existing_name in new_name_to_cids and client_id in new_name_to_cids[existing_name]:
                new_name_to_cids[existing_name].remove(client_id)
                if not new_name_to_cids[existing_name]:
                    del new_name_to_cids[existing_name]
            new_cid_to_name[client_id] = full_name
            new_cid_to_email[client_id] = derive_user_email(first, client_id)
            if not is_generic_name:
                new_name_to_cids.setdefault(full_name, []).append(client_id)
        elif not is_generic_name and full_name and client_id not in new_name_to_cids.get(full_name, []):
            new_name_to_cids.setdefault(full_name, []).append(client_id)

        if client_id not in new_cid_to_email:
            new_cid_to_email[client_id] = derive_user_email(first, client_id)

        uid = str(row.get("user_id") or "").strip()
        if uid and full_name and not is_generic_name:
            new_cid_to_name[uid] = full_name

        # Credential deduplication: prefer row with newer access_token_created_at or higher row_id
        existing = new_client_creds.get(client_id)
        if existing:
            existing_time = existing.get("token_created_at") or ""
            if existing_time and token_created and existing_time > token_created:
                # Keep existing record because it has a newer token
                continue

        # Preserve live in-memory session token if present; only fallback to CSV token
        live_cred = _client_creds.get(client_id)
        live_token = (live_cred.get("token") or "") if live_cred else ""
        live_token_created = (live_cred.get("token_created_at") or "") if live_cred else ""

        effective_token = live_token if live_token else token_val
        effective_token_created = live_token_created if live_token else token_created

        cred_record = {
            "client_id": client_id,
            "password": creds_json.get("password") or "",
            "api_key": creds_json.get("api_key") or creds_json.get("app_id") or creds_json.get("app_key") or creds_json.get("app_code") or "",
            "api_secret": creds_json.get("api_secret") or creds_json.get("secret_key") or "",
            "totp_secret": creds_json.get("totp_secret") or creds_json.get("totp_secret_key") or creds_json.get("totp") or "",
            "static_ip": static_ip,
            "broker": broker,
            "token": effective_token,
            "token_created_at": effective_token_created,
            "user_id": int(uid) if uid and uid.isdigit() else None,
            "full_name": new_cid_to_name.get(client_id, full_name),
            "row_id": row_id,
        }
        new_client_creds[client_id] = cred_record

    for cid, cred in new_client_creds.items():
        if cid in new_cid_to_name:
            cred["full_name"] = new_cid_to_name[cid]

    synced_records = list(new_client_creds.values())

    if not new_configured_cids:
        logger.warning("No valid client credentials parsed from remote CSV.")
        await _fallback_load_from_db()
        return len(_configured_cids)

    # Update in-memory caches
    _configured_cids = new_configured_cids
    _cid_to_name = new_cid_to_name
    _cid_to_email = new_cid_to_email
    _name_to_cids = new_name_to_cids
    _client_creds = new_client_creds
    _initialized = True

    # Synchronize into SQLite database
    upserted_count = 0
    async with get_db() as db:
        for rec in synced_records:
            fingerprint = f"{rec['broker']}:{rec['client_id']}"
            upsert_query = """
                INSERT INTO credentials 
                (broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'working', ?, datetime('now', 'localtime'), ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    status = 'working',
                    token = CASE 
                        WHEN credentials.token IS NOT NULL AND credentials.token != '' THEN credentials.token 
                        ELSE excluded.token 
                    END,
                    token_updated_at = CASE 
                        WHEN credentials.token IS NOT NULL AND credentials.token != '' THEN credentials.token_updated_at 
                        ELSE excluded.token_updated_at 
                    END,
                    api_key = excluded.api_key,
                    api_secret = excluded.api_secret,
                    totp_secret = excluded.totp_secret,
                    password = excluded.password,
                    static_ip = excluded.static_ip,
                    user_id = COALESCE(excluded.user_id, credentials.user_id)
            """
            await db.execute(upsert_query, (
                rec["broker"],
                rec["client_id"],
                rec["api_key"],
                rec["api_secret"],
                rec["totp_secret"],
                rec["password"],
                rec["static_ip"],
                fingerprint,
                rec["token"],
                rec["user_id"],
            ))
            upserted_count += 1
        await db.commit()

    logger.info(f"✅ Successfully synced {upserted_count} credentials from remote CSV into SQLite.")
    await logger_system.info(f"🌐 Dynamic credentials loaded: {len(_configured_cids)} account(s) ready.")
    return len(_configured_cids)


async def _fallback_load_from_db():
    """Fallback: populates in-memory maps from existing SQLite credentials table if URL is unreachable."""
    global _configured_cids, _cid_to_name, _name_to_cids, _client_creds, _initialized
    try:
        async with get_db() as db:
            query = """
                SELECT client_id, broker, password, api_key, api_secret, totp_secret, static_ip, token, user_id
                FROM credentials
                WHERE status = 'working' AND client_id IS NOT NULL AND TRIM(client_id) != ''
            """
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()

            for row in rows:
                cid = str(row[0]).strip()
                _configured_cids.add(cid)
                _client_creds[cid] = {
                    "client_id": cid,
                    "broker": row[1],
                    "password": row[2] or "",
                    "api_key": row[3] or "",
                    "api_secret": row[4] or "",
                    "totp_secret": row[5] or "",
                    "static_ip": row[6] or "",
                    "token": row[7] or "",
                    "user_id": row[8],
                }

        from utils.user_mapping import DEFAULT_CLIENT_NAMES
        for cid, name in DEFAULT_CLIENT_NAMES.items():
            if cid not in _cid_to_name:
                _cid_to_name[cid] = name
            if name not in _name_to_cids:
                _name_to_cids[name] = []
            if cid not in _name_to_cids[name]:
                _name_to_cids[name].append(cid)

        _initialized = True
        logger.info(f"Loaded {len(_configured_cids)} credentials from SQLite fallback.")
    except Exception as e:
        logger.error(f"Fallback SQLite credential load failed: {e}")


def get_dynamic_configured_cids() -> Set[str]:
    """Returns the set of active client IDs configured dynamically."""
    return set(_configured_cids)


def get_dynamic_username(client_id: str) -> str:
    """Returns the username for a client ID, falling back to client_id if not mapped."""
    if not client_id:
        return ""
    return _cid_to_name.get(str(client_id), str(client_id))


def get_client_email(client_id: str) -> str:
    """Returns the derived user email (e.g. shivram@gmail.com) for a given client_id."""
    if not client_id:
        return ""
    cid_str = str(client_id).strip()
    return _cid_to_email.get(cid_str, f"{cid_str.lower()}@gmail.com")


def get_dynamic_cids_for_user(query_val: str) -> List[str]:
    """Returns all client IDs associated with a username or client ID."""
    if not query_val:
        return []
    if query_val in _name_to_cids:
        return _name_to_cids[query_val]
    return [query_val]


def compute_credential_signature(cred: Optional[dict]) -> str:
    """Computes a SHA-256 hash of sensitive auth fields to detect edits in Dropbox / static CSV."""
    if not cred:
        return ""
    broker = str(cred.get("broker") or "").strip().upper()
    client_id = str(cred.get("client_id") or "").strip().upper()
    api_key = str(cred.get("api_key") or "").strip()
    api_secret = str(cred.get("api_secret") or "").strip()
    totp_secret = str(cred.get("totp_secret") or "").strip()
    password = str(cred.get("password") or "").strip()
    static_ip = str(cred.get("static_ip") or "").strip()
    raw = f"{broker}:{client_id}:{api_key}:{api_secret}:{totp_secret}:{password}:{static_ip}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_dynamic_credentials(client_id: str) -> Optional[dict]:
    """Retrieves fresh credential data for a given client_id directly from the source of truth cache (Dropbox or static CSV)."""
    if not client_id:
        return None
    cid_str = str(client_id).strip()
    return _client_creds.get(cid_str)


def update_dynamic_credential_token(client_id: str, token: str) -> None:
    """Updates the runtime session token for an active credential in memory."""
    if not client_id or not token:
        return
    cid_str = str(client_id).strip()
    if cid_str in _client_creds:
        _client_creds[cid_str]["token"] = str(token).strip()


def get_credential_source_info() -> dict:
    """Returns the current credentials source status and configuration."""
    use_dropbox = getattr(settings, "USE_DROPBOX_CREDENTIALS", False)
    dropbox_url = getattr(settings, "DROPBOX_CREDENTIALS_URL", "") or getattr(settings, "CREDENTIALS_CSV_URL", "")
    csv_path = getattr(settings, "CREDENTIALS_CSV_PATH", "static/plato_client_credentials.csv")
    return {
        "use_dropbox": bool(use_dropbox),
        "active_source": "DROPBOX" if use_dropbox else "LOCAL_CSV",
        "dropbox_url": dropbox_url,
        "csv_path": csv_path,
        "configured_accounts": len(_configured_cids)
    }
