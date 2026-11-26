import logging
from typing import Optional
from database.db import get_db
from models.schemas import Credential

logger = logging.getLogger(__name__)

async def save_working_credential(cred: Credential) -> None:
    """UPSERTs a working credential and marks others in the dedup group as skipped."""
    async with get_db() as db:
        # The dedup group is defined by broker + client_id + static_ip
        
        # First, mark any existing active credentials in this group as 'skipped'
        # to ensure only one 'working' credential per group.
        update_query = """
            UPDATE credentials 
            SET status = 'skipped' 
            WHERE broker = ? AND client_id = ? AND static_ip = ? AND id != ?
        """
        # We use -1 if id is None to ensure we don't skip ourselves if we exist
        current_id = cred.id if cred.id is not None else -1
        await db.execute(update_query, (cred.broker, cred.client_id, str(cred.static_ip), current_id))
        
        # Now UPSERT the working credential
        upsert_query = """
            INSERT INTO credentials 
            (broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at, sid, base_url, sub_account_id, public_key, refresh_token, mobile_number, mpin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'working', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                status = 'working',
                token = excluded.token,
                token_updated_at = excluded.token_updated_at,
                api_key = excluded.api_key,
                api_secret = excluded.api_secret,
                password = excluded.password,
                sid = CASE WHEN excluded.sid IS NOT NULL AND excluded.sid != '' THEN excluded.sid ELSE credentials.sid END,
                base_url = CASE WHEN excluded.base_url IS NOT NULL AND excluded.base_url != '' THEN excluded.base_url ELSE credentials.base_url END,
                sub_account_id = CASE WHEN excluded.sub_account_id IS NOT NULL AND excluded.sub_account_id != '' THEN excluded.sub_account_id ELSE credentials.sub_account_id END,
                public_key = CASE WHEN excluded.public_key IS NOT NULL AND excluded.public_key != '' THEN excluded.public_key ELSE credentials.public_key END,
                refresh_token = CASE WHEN excluded.refresh_token IS NOT NULL AND excluded.refresh_token != '' THEN excluded.refresh_token ELSE credentials.refresh_token END,
                mobile_number = CASE WHEN excluded.mobile_number IS NOT NULL AND excluded.mobile_number != '' THEN excluded.mobile_number ELSE credentials.mobile_number END,
                mpin = CASE WHEN excluded.mpin IS NOT NULL AND excluded.mpin != '' THEN excluded.mpin ELSE credentials.mpin END
        """
        await db.execute(upsert_query, (
            cred.broker, cred.client_id, cred.api_key, cred.api_secret, cred.totp_secret, 
            cred.password, str(cred.static_ip), cred.fingerprint, 
            cred.token, cred.token_updated_at,
            getattr(cred, "sid", "") or "",
            getattr(cred, "base_url", "") or "",
            getattr(cred, "sub_account_id", "") or "",
            getattr(cred, "public_key", "") or "",
            getattr(cred, "refresh_token", "") or "",
            getattr(cred, "mobile_number", "") or "",
            getattr(cred, "mpin", "") or "",
        ))
        
        await db.commit()
        logger.info(f"Saved working credential for {cred.broker}:{cred.client_id}")

        # Keep in-memory cache synchronized with the latest runtime token
        try:
            from credentials.dynamic_loader import update_dynamic_credential_token
            from datetime import datetime
            if isinstance(cred.token_updated_at, datetime):
                upd_time = cred.token_updated_at.strftime("%Y-%m-%d %H:%M:%S")
            elif cred.token_updated_at:
                upd_time = str(cred.token_updated_at)
            else:
                upd_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            update_dynamic_credential_token(
                cred.client_id,
                cred.token,
                upd_time,
                sid=getattr(cred, "sid", "") or "",
                base_url=getattr(cred, "base_url", "") or "",
                sub_account_id=getattr(cred, "sub_account_id", "") or "",
                public_key=getattr(cred, "public_key", "") or "",
                refresh_token=getattr(cred, "refresh_token", "") or "",
            )
        except Exception:
            pass

