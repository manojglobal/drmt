"""
credentials/dynamic_loader.py — Dynamic Credential Fetcher & Database Sync.
Downloads client credentials in strictly read-only mode from Dropbox OAuth API
(or local static CSV when USE_DROPBOX_CREDENTIALS is False), parses broker
credentials, populates in-memory caches, and synchronizes with SQLite.
"""

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
import time
from typing import Dict, List, Optional, Set, Tuple

import aiohttp

from database.db import get_db
from logger.logger import logger_auth, logger_system, sanitize_log_message
from settings import settings

logger = logging.getLogger("DRMT.CredentialsSync")

_IST = timezone(timedelta(hours=5, minutes=30))


def _parse_token_created_dt(ts_str: str) -> Optional[datetime]:
    """Parses token creation/update timestamps into naive-IST datetimes for comparison."""
    raw = str(ts_str or "").strip()
    if not raw:
        return None
    norm = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is not None:
            dt = dt.astimezone(_IST).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _resolve_effective_token(
    csv_token: str,
    csv_created: str,
    live_cred: Optional[dict],
) -> Tuple[str, str]:
    """Chooses the newer token between CSV source data and live in-memory session state."""
    if not live_cred:
        return csv_token, csv_created

    live_token = str(live_cred.get("token") or "").strip()
    live_created = str(live_cred.get("token_created_at") or "").strip()
    if not live_token:
        return csv_token, csv_created
    if not csv_token:
        return live_token, live_created

    has_csv_lineage = "csv_token" in live_cred
    prev_csv_token = str(live_cred.get("csv_token") or "").strip() if has_csv_lineage else ""

    # Operator explicitly edited the token in CSV/Dropbox since last sync
    if has_csv_lineage and csv_token != prev_csv_token:
        return csv_token, csv_created

    csv_dt = _parse_token_created_dt(csv_created)
    live_dt = _parse_token_created_dt(live_created)

    if csv_token == live_token:
        if csv_dt is not None and (live_dt is None or csv_dt > live_dt):
            return csv_token, csv_created
        return live_token, live_created or csv_created

    if csv_dt is not None and live_dt is not None:
        return (csv_token, csv_created) if csv_dt >= live_dt else (live_token, live_created)
    if csv_dt is not None and live_dt is None:
        return csv_token, csv_created
    # CSV stayed unchanged while runtime auto-refresh generated a newer live token
    if (has_csv_lineage and csv_token == prev_csv_token) or live_dt is not None:
        return live_token, live_created
    return csv_token, csv_created

_DROPBOX_OAUTH_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
_DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"

# In-memory caches populated dynamically
_configured_cids: Set[str] = set()
_cid_to_name: Dict[str, str] = {}
_cid_to_email: Dict[str, str] = {}
_name_to_cids: Dict[str, List[str]] = {}
_client_creds: Dict[str, dict] = {}
_initialized: bool = False

# In-memory short-lived Dropbox access token cache (refreshed via refresh_token)
_dropbox_access_token: str = ""
_dropbox_token_expires_at: float = 0.0
_dropbox_token_cache_key: str = ""


def derive_user_email(first_name: str, client_id: str = "") -> str:
    """Derives user email matching historical Plato format using first_name only (e.g. shivram@gmail.com, recover@gmail.com)."""
    fn = (first_name or "").strip().lower()
    cid = (client_id or "").strip().upper()
    if "recover" in fn or cid == "P645M6":
        return "recover@gmail.com"
    if fn:
        clean = "".join(ch for ch in fn.split()[0] if ch.isalnum())
        if clean:
            return f"{clean}@gmail.com"
    return ""


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


def _is_html_response(content_type: str, body: str) -> bool:
    """Returns True if an HTTP response is an HTML page instead of CSV data."""
    if "text/html" in (content_type or "").lower():
        return True
    head = (body or "").lstrip("\ufeff \t\r\n")[:64].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def _has_dropbox_oauth_credentials() -> bool:
    """Returns True when Dropbox OAuth App Key, App Secret, and Refresh Token are configured."""
    return bool(
        str(getattr(settings, "DRMT_DROPBOX_APP_KEY", "") or "").strip()
        and str(getattr(settings, "DRMT_DROPBOX_APP_SECRET", "") or "").strip()
        and str(getattr(settings, "DRMT_DROPBOX_REFRESH_TOKEN", "") or "").strip()
    )


def _normalize_dropbox_api_path(file_path: str) -> str:
    """Ensures Dropbox API file path starts with '/'."""
    cleaned = (file_path or "/plato_client_credentials.csv").strip()
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    return cleaned


async def _get_dropbox_access_token(
    session: aiohttp.ClientSession, force_refresh: bool = False
) -> str:
    """Exchanges the configured Dropbox refresh token for a short-lived read access token."""
    global _dropbox_access_token, _dropbox_token_expires_at, _dropbox_token_cache_key

    app_key = str(getattr(settings, "DRMT_DROPBOX_APP_KEY", "") or "").strip()
    app_secret = str(getattr(settings, "DRMT_DROPBOX_APP_SECRET", "") or "").strip()
    refresh_token = str(getattr(settings, "DRMT_DROPBOX_REFRESH_TOKEN", "") or "").strip()
    cache_key = f"{app_key}:{refresh_token}"

    now = time.monotonic()
    if (
        not force_refresh
        and _dropbox_access_token
        and _dropbox_token_cache_key == cache_key
        and now < (_dropbox_token_expires_at - 60.0)
    ):
        return _dropbox_access_token

    form_data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": app_key,
        "client_secret": app_secret,
    }
    async with session.post(_DROPBOX_OAUTH_TOKEN_URL, data=form_data) as resp:
        if resp.status != 200:
            err_body = await resp.text(encoding="utf-8", errors="replace")
            raise RuntimeError(
                f"Dropbox OAuth token refresh failed (HTTP {resp.status}): {sanitize_log_message(err_body[:200])}"
            )
        token_payload = await resp.json(content_type=None)

    access_token = str((token_payload or {}).get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Dropbox OAuth response missing access_token")

    expires_in = int((token_payload or {}).get("expires_in") or 14400)
    _dropbox_access_token = access_token
    _dropbox_token_expires_at = now + max(120, expires_in)
    _dropbox_token_cache_key = cache_key
    return _dropbox_access_token


async def _fetch_csv_from_dropbox_api() -> str:
    """Downloads credentials CSV from Dropbox via read-only /2/files/download API."""
    global _dropbox_access_token, _dropbox_token_expires_at

    dropbox_path = _normalize_dropbox_api_path(
        getattr(settings, "DRMT_DROPBOX_FILE_PATH", "/plato_client_credentials.csv")
    )
    logger.info(f"🌐 [Credentials Sync] Source: DROPBOX OAUTH READ-ONLY API (Path: {dropbox_path})")

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(2):
            access_token = await _get_dropbox_access_token(session, force_refresh=(attempt > 0))
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Dropbox-API-Arg": json.dumps({"path": dropbox_path}),
            }
            async with session.post(_DROPBOX_DOWNLOAD_URL, headers=headers) as resp:
                if resp.status == 401 and attempt == 0:
                    _dropbox_access_token = ""
                    _dropbox_token_expires_at = 0.0
                    continue
                content_type = resp.headers.get("Content-Type", "")
                raw_text = await resp.text(encoding="utf-8-sig", errors="replace")
                if resp.status != 200:
                    raise RuntimeError(
                        f"Dropbox download failed (HTTP {resp.status}) for {dropbox_path}: "
                        f"{sanitize_log_message(raw_text[:200])}"
                    )
                if _is_html_response(content_type, raw_text):
                    raise RuntimeError("Dropbox returned HTML instead of CSV content")
                logger.info(
                    f"✅ Successfully read credentials CSV from Dropbox API ({len(raw_text)} bytes)"
                )
                return raw_text

    raise RuntimeError(f"Dropbox download failed for {dropbox_path}")


async def _fetch_csv_from_dropbox_url(target_url: str) -> str:
    """Downloads credentials CSV from a direct Dropbox share URL (read-only fallback)."""
    normalized_url = _normalize_dropbox_url(target_url)
    logger.info(f"🌐 [Credentials Sync] Source: DROPBOX URL (URL: {normalized_url})")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(normalized_url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} while fetching Dropbox credentials CSV")
            content_type = resp.headers.get("Content-Type", "")
            raw_csv_text = await resp.text(encoding="utf-8-sig", errors="replace")
            if _is_html_response(content_type, raw_csv_text):
                raise RuntimeError(
                    "Dropbox URL returned an HTML page instead of CSV (file may be deleted or link expired)"
                )
    logger.info(f"✅ Successfully downloaded credentials CSV from Dropbox URL ({len(raw_csv_text)} bytes)")
    return raw_csv_text


def _read_local_csv_text() -> Tuple[str, Optional[str]]:
    """Reads credentials CSV from the local static file in strictly read-only mode."""
    from utils.paths import get_app_dir

    configured_path = getattr(settings, "CREDENTIALS_CSV_PATH", "static/plato_client_credentials.csv")
    local_csv_path = None
    if configured_path:
        candidate_paths = [
            configured_path if os.path.isabs(configured_path) else os.path.join(get_app_dir(), configured_path),
            os.path.join(os.getcwd(), configured_path),
            os.path.join(get_app_dir(), "static", "plato_client_credentials.csv"),
        ]
        for p in candidate_paths:
            if os.path.isfile(p):
                local_csv_path = p
                break

    if not local_csv_path:
        return "", None

    logger.info(f"📄 [Credentials Sync] Source: LOCAL CSV ({local_csv_path})")
    with open(local_csv_path, "r", encoding="utf-8-sig") as f:
        return f.read(), local_csv_path


def _parse_csv_credentials(
    raw_csv_text: str,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, List[str]], Set[str], Dict[str, dict]]:
    """Parses raw CSV text into deduplicated credential maps without modifying any files."""
    if not raw_csv_text or not raw_csv_text.strip() or _is_html_response("", raw_csv_text):
        return {}, {}, {}, set(), {}

    cleaned_csv = raw_csv_text.lstrip("\ufeff")
    reader = list(csv.DictReader(io.StringIO(cleaned_csv)))

    def _safe_row_id(row: dict) -> int:
        try:
            return int(str(row.get("id") or "0").strip() or "0")
        except ValueError:
            return 0

    # Sort by numeric ID ascending so newer entries naturally evaluate later
    reader.sort(key=_safe_row_id)

    new_cid_to_name: Dict[str, str] = {}
    new_cid_to_email: Dict[str, str] = {}
    new_name_to_cids: Dict[str, List[str]] = {}
    new_configured_cids: Set[str] = set()
    new_client_creds: Dict[str, dict] = {}

    for row in reader:
        if not row.get("id") or not row.get("broker_creds"):
            continue

        first = (row.get("first_name") or row.get("firstName") or "").strip()
        last = (row.get("last_name") or row.get("lastName") or "").strip()
        full_name = f"{first} {last}".strip() if last else first
        if not first and full_name:
            first = full_name.split()[0]
        broker = str(row.get("broker") or "").strip().upper()
        static_ip = str(row.get("static_ip") or "").strip()

        try:
            creds_json = json.loads(row.get("broker_creds") or "{}")
        except Exception:
            continue

        client_id = (
            creds_json.get("client_id")
            or creds_json.get("sub_account_id")
            or creds_json.get("subAccountId")
            or creds_json.get("api_client_id")
            or creds_json.get("clientcode")
            or creds_json.get("uid")
        )
        if not client_id:
            continue

        client_id = str(client_id).strip()
        new_configured_cids.add(client_id)

        token_created = str(creds_json.get("access_token_created_at") or "").strip()
        token_val = str(creds_json.get("access_token") or creds_json.get("token") or "").strip()
        row_id = _safe_row_id(row)

        # Name deduplication: prefer human names over generic labels (e.g. Demat, Virtual, Recovery)
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
            email_val = derive_user_email(first, client_id)
            if email_val:
                new_cid_to_email[client_id] = email_val
            if not is_generic_name:
                new_name_to_cids.setdefault(full_name, []).append(client_id)
        elif not is_generic_name and full_name and client_id not in new_name_to_cids.get(full_name, []):
            new_name_to_cids.setdefault(full_name, []).append(client_id)

        email_val = derive_user_email(first, client_id)
        if email_val and (client_id not in new_cid_to_email or not new_cid_to_email[client_id]):
            new_cid_to_email[client_id] = email_val

        uid = str(row.get("user_id") or "").strip()
        if uid and full_name and not is_generic_name:
            new_cid_to_name[uid] = full_name

        # Credential deduplication: prefer row with newer access_token_created_at or higher row_id
        existing = new_client_creds.get(client_id)
        if existing:
            existing_time = existing.get("token_created_at") or ""
            if existing_time and token_created and existing_time > token_created:
                continue

        # Select the fresher token between CSV and runtime memory without losing edits or newer logins
        live_cred = _client_creds.get(client_id)
        effective_token, effective_token_created = _resolve_effective_token(
            token_val, token_created, live_cred
        )

        def _csv_or_live(csv_val: str, field_name: str) -> str:
            cleaned = str(csv_val or "").strip()
            if cleaned:
                return cleaned
            return str(live_cred.get(field_name) or "") if live_cred else ""

        cred_record = {
            "client_id": client_id,
            "password": creds_json.get("password") or "",
            "api_key": creds_json.get("api_key") or creds_json.get("app_id") or creds_json.get("app_key") or creds_json.get("app_code") or creds_json.get("api_token") or creds_json.get("api_client_id") or creds_json.get("apiClientId") or "",
            "api_secret": creds_json.get("api_secret") or creds_json.get("secret_key") or creds_json.get("authorization_key") or creds_json.get("authorizationKey") or "",
            "totp_secret": creds_json.get("totp_secret") or creds_json.get("totp_secret_key") or creds_json.get("totp") or "",
            "static_ip": static_ip,
            "broker": broker,
            "token": effective_token,
            "token_created_at": effective_token_created,
            "csv_token": token_val,
            "user_id": int(uid) if uid and uid.isdigit() else None,
            "full_name": new_cid_to_name.get(client_id, full_name),
            "row_id": row_id,
            "sid": _csv_or_live(creds_json.get("sid") or "", "sid"),
            "base_url": _csv_or_live(creds_json.get("base_url") or "", "base_url"),
            "sub_account_id": _csv_or_live(creds_json.get("sub_account_id") or creds_json.get("subAccountId") or "", "sub_account_id"),
            "public_key": _csv_or_live(creds_json.get("public_key") or creds_json.get("publicKey") or "", "public_key"),
            "refresh_token": _csv_or_live(creds_json.get("refresh_token") or creds_json.get("refreshToken") or "", "refresh_token"),
            "mobile_number": (creds_json.get("mobile_number") or creds_json.get("phone_number") or "").strip(),
            "mpin": str(creds_json.get("mpin") or "").strip(),
        }
        new_client_creds[client_id] = cred_record

    for cid, cred in new_client_creds.items():
        if cid in new_cid_to_name:
            cred["full_name"] = new_cid_to_name[cid]

    return new_cid_to_name, new_cid_to_email, new_name_to_cids, new_configured_cids, new_client_creds


async def fetch_and_sync_credentials(url: Optional[str] = None) -> int:
    """
    Fetches credentials from Dropbox (when USE_DROPBOX_CREDENTIALS=True) or
    local static CSV (when False) in strictly read-only mode, updates in-memory
    mappings, and UPSERTs active accounts into the SQLite credentials table.
    """
    global _configured_cids, _cid_to_name, _cid_to_email, _name_to_cids, _client_creds, _initialized

    use_dropbox = bool(getattr(settings, "USE_DROPBOX_CREDENTIALS", False))
    parsed = ({}, {}, {}, set(), {})

    # ── BRANCH 1: DROPBOX READ-ONLY SOURCE (when USE_DROPBOX_CREDENTIALS is True) ──
    if use_dropbox:
        try:
            if url:
                raw_csv_text = await _fetch_csv_from_dropbox_url(url)
            elif _has_dropbox_oauth_credentials():
                raw_csv_text = await _fetch_csv_from_dropbox_api()
            else:
                fallback_url = getattr(settings, "DROPBOX_CREDENTIALS_URL", "") or getattr(settings, "CREDENTIALS_CSV_URL", "")
                if not fallback_url:
                    raise RuntimeError(
                        "USE_DROPBOX_CREDENTIALS is True, but neither Dropbox OAuth credentials nor DROPBOX_CREDENTIALS_URL is configured"
                    )
                raw_csv_text = await _fetch_csv_from_dropbox_url(fallback_url)

            parsed = _parse_csv_credentials(raw_csv_text)
            if not parsed[3]:
                raise RuntimeError("No valid client credentials parsed from Dropbox CSV")
        except Exception as fetch_err:
            logger.error(f"❌ Failed to read credentials from Dropbox: {fetch_err}. Falling back to local static CSV.")
            await logger_auth.warn(f"Failed to read credentials from Dropbox: {fetch_err}. Falling back to local static CSV.")
            parsed = ({}, {}, {}, set(), {})

    # ── BRANCH 2: LOCAL STATIC CSV (when USE_DROPBOX_CREDENTIALS is False or Dropbox failed) ──
    if not parsed[3]:
        try:
            raw_csv_text, local_csv_path = _read_local_csv_text()
            if not local_csv_path:
                logger.warning("No credentials CSV file found. Falling back to SQLite cache.")
                await _fallback_load_from_db()
                return len(_configured_cids)
            if not raw_csv_text or not raw_csv_text.strip():
                logger.warning("Credentials CSV is empty. Falling back to SQLite cache.")
                await _fallback_load_from_db()
                return len(_configured_cids)
            parsed = _parse_csv_credentials(raw_csv_text)
        except Exception as file_err:
            logger.error(f"❌ Failed to read local static credentials CSV: {file_err}")
            await logger_auth.error(f"Failed to read local static credentials CSV: {file_err}. Falling back to SQLite cache.")
            await _fallback_load_from_db()
            return len(_configured_cids)

    new_cid_to_name, new_cid_to_email, new_name_to_cids, new_configured_cids, new_client_creds = parsed
    synced_records = list(new_client_creds.values())

    if not new_configured_cids:
        logger.warning("No valid client credentials parsed from CSV. Falling back to SQLite cache.")
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
                (broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at, user_id, sid, base_url, sub_account_id, public_key, refresh_token, mobile_number, mpin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'working', ?, COALESCE(?, datetime('now', 'localtime')), ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    status = 'working',
                    token = CASE 
                        WHEN excluded.token IS NOT NULL AND excluded.token != '' THEN excluded.token 
                        ELSE credentials.token 
                    END,
                    token_updated_at = CASE 
                        WHEN excluded.token IS NOT NULL AND excluded.token != '' THEN excluded.token_updated_at 
                        ELSE credentials.token_updated_at 
                    END,
                    api_key = excluded.api_key,
                    api_secret = excluded.api_secret,
                    totp_secret = excluded.totp_secret,
                    password = excluded.password,
                    static_ip = excluded.static_ip,
                    user_id = COALESCE(excluded.user_id, credentials.user_id),
                    sid = CASE WHEN excluded.sid IS NOT NULL AND excluded.sid != '' THEN excluded.sid ELSE credentials.sid END,
                    base_url = CASE WHEN excluded.base_url IS NOT NULL AND excluded.base_url != '' THEN excluded.base_url ELSE credentials.base_url END,
                    sub_account_id = CASE WHEN excluded.sub_account_id IS NOT NULL AND excluded.sub_account_id != '' THEN excluded.sub_account_id ELSE credentials.sub_account_id END,
                    public_key = CASE WHEN excluded.public_key IS NOT NULL AND excluded.public_key != '' THEN excluded.public_key ELSE credentials.public_key END,
                    refresh_token = CASE WHEN excluded.refresh_token IS NOT NULL AND excluded.refresh_token != '' THEN excluded.refresh_token ELSE credentials.refresh_token END,
                    mobile_number = CASE WHEN excluded.mobile_number IS NOT NULL AND excluded.mobile_number != '' THEN excluded.mobile_number ELSE credentials.mobile_number END,
                    mpin = CASE WHEN excluded.mpin IS NOT NULL AND excluded.mpin != '' THEN excluded.mpin ELSE credentials.mpin END
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
                rec.get("token_created_at") or None,
                rec["user_id"],
                rec.get("sid", ""),
                rec.get("base_url", ""),
                rec.get("sub_account_id", ""),
                rec.get("public_key", ""),
                rec.get("refresh_token", ""),
                rec.get("mobile_number", ""),
                rec.get("mpin", ""),
            ))
            upserted_count += 1

        # Mark credentials in SQLite not present in active CSV as archived
        if new_configured_cids:
            placeholders = ",".join("?" for _ in new_configured_cids)
            archive_query = f"""
                UPDATE credentials 
                SET status = 'archived' 
                WHERE status = 'working' 
                  AND UPPER(broker) != 'FAKE'
                  AND client_id NOT IN ({placeholders})
            """
            await db.execute(archive_query, list(new_configured_cids))

        await db.commit()

    source_label = "Dropbox" if use_dropbox and parsed[3] else "CSV"
    logger.info(f"✅ Successfully synced {upserted_count} credentials from {source_label} into SQLite.")
    await logger_system.info(f"🌐 Dynamic credentials loaded: {len(_configured_cids)} account(s) ready.")
    return len(_configured_cids)


async def _fallback_load_from_db():
    """Fallback: populates in-memory maps from existing SQLite credentials table if URL is unreachable."""
    global _configured_cids, _cid_to_name, _name_to_cids, _client_creds, _initialized
    try:
        async with get_db() as db:
            query = """
                SELECT client_id, broker, password, api_key, api_secret, totp_secret, static_ip, token, user_id, sid, base_url, sub_account_id, public_key, refresh_token, mobile_number, mpin, token_updated_at
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
                    "sid": row[9] or "" if len(row) > 9 else "",
                    "base_url": row[10] or "" if len(row) > 10 else "",
                    "sub_account_id": row[11] or "" if len(row) > 11 else "",
                    "public_key": row[12] or "" if len(row) > 12 else "",
                    "refresh_token": row[13] or "" if len(row) > 13 else "",
                    "mobile_number": row[14] or "" if len(row) > 14 else "",
                    "mpin": row[15] or "" if len(row) > 15 else "",
                    "token_created_at": str(row[16]) if len(row) > 16 and row[16] else "",
                }

        from utils.user_mapping import DEFAULT_CLIENT_NAMES
        for cid, name in DEFAULT_CLIENT_NAMES.items():
            if cid not in _cid_to_name:
                _cid_to_name[cid] = name
            if name not in _name_to_cids:
                _name_to_cids[name] = []
            if cid not in _name_to_cids[name]:
                _name_to_cids[name].append(cid)
            if cid not in _cid_to_email and name:
                fn = name.split()[0]
                em = derive_user_email(fn, cid)
                if em:
                    _cid_to_email[cid] = em

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
    return _cid_to_email.get(cid_str, "")


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
    sid = str(cred.get("sid") or "").strip()
    base_url = str(cred.get("base_url") or "").strip()
    sub_account_id = str(cred.get("sub_account_id") or "").strip()
    public_key = str(cred.get("public_key") or "").strip()
    refresh_token = str(cred.get("refresh_token") or "").strip()
    token = str(cred.get("token") or "").strip()
    mobile_number = str(cred.get("mobile_number") or "").strip()
    mpin = str(cred.get("mpin") or "").strip()
    raw = (
        f"{broker}:{client_id}:{api_key}:{api_secret}:{totp_secret}:{password}:"
        f"{static_ip}:{sid}:{base_url}:{sub_account_id}:{public_key}:{refresh_token}:"
        f"{token}:{mobile_number}:{mpin}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_dynamic_credentials(client_id: str) -> Optional[dict]:
    """Retrieves fresh credential data for a given client_id directly from the source of truth cache (Dropbox or static CSV)."""
    if not client_id:
        return None
    cid_str = str(client_id).strip()
    return _client_creds.get(cid_str)


def update_dynamic_credential_token(
    client_id: str,
    token: str,
    token_created_at: Optional[str] = None,
    sid: Optional[str] = None,
    base_url: Optional[str] = None,
    sub_account_id: Optional[str] = None,
    public_key: Optional[str] = None,
    refresh_token: Optional[str] = None,
) -> None:
    """Updates the runtime session token and session metadata for an active credential in memory."""
    if not client_id:
        return
    cid_str = str(client_id).strip()
    if cid_str not in _client_creds:
        return
    target = _client_creds[cid_str]
    if token:
        target["token"] = str(token).strip()
        target["token_created_at"] = token_created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for field_name, field_val in (
        ("sid", sid),
        ("base_url", base_url),
        ("sub_account_id", sub_account_id),
        ("public_key", public_key),
        ("refresh_token", refresh_token),
    ):
        if field_val is not None and str(field_val).strip():
            target[field_name] = str(field_val).strip()


def get_credential_source_info() -> dict:
    """Returns the current credentials source status and configuration."""
    use_dropbox = getattr(settings, "USE_DROPBOX_CREDENTIALS", False)
    dropbox_url = getattr(settings, "DROPBOX_CREDENTIALS_URL", "") or getattr(settings, "CREDENTIALS_CSV_URL", "")
    csv_path = getattr(settings, "CREDENTIALS_CSV_PATH", "static/plato_client_credentials.csv")
    return {
        "use_dropbox": bool(use_dropbox),
        "active_source": "DROPBOX" if use_dropbox else "LOCAL_CSV",
        "dropbox_auth_mode": "OAUTH_READONLY" if _has_dropbox_oauth_credentials() else "URL",
        "dropbox_file_path": _normalize_dropbox_api_path(
            getattr(settings, "DRMT_DROPBOX_FILE_PATH", "/plato_client_credentials.csv")
        ),
        "dropbox_url": dropbox_url,
        "csv_path": csv_path,
        "configured_accounts": len(_configured_cids),
    }
