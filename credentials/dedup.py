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
            (broker, client_id, api_key, api_secret, totp_secret, password, static_ip, fingerprint, status, token, token_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'working', ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                status = 'working',
                token = excluded.token,
                token_updated_at = excluded.token_updated_at,
                api_key = excluded.api_key,
                api_secret = excluded.api_secret,
                password = excluded.password
        """
        await db.execute(upsert_query, (
            cred.broker, cred.client_id, cred.api_key, cred.api_secret, cred.totp_secret, 
            cred.password, str(cred.static_ip), cred.fingerprint, 
            cred.token, cred.token_updated_at
        ))
        
        await db.commit()
        logger.info(f"Saved working credential for {cred.broker}:{cred.client_id}")

