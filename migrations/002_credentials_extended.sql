-- 002_credentials_extended.sql
-- Extend credentials table with broker-specific fields for token generation

-- Add new columns to credentials table
ALTER TABLE credentials ADD COLUMN client_id TEXT;
ALTER TABLE credentials ADD COLUMN password TEXT;
ALTER TABLE credentials ADD COLUMN totp_secret TEXT;
ALTER TABLE credentials ADD COLUMN api_key_encrypted TEXT;  -- for MSTOCK api_key
ALTER TABLE credentials ADD COLUMN app_code TEXT;           -- for ALICE BLUE
ALTER TABLE credentials ADD COLUMN access_token TEXT;
ALTER TABLE credentials ADD COLUMN access_token_created_at DATETIME;
ALTER TABLE credentials ADD COLUMN user_id INTEGER;         -- reference to Plato user_id
ALTER TABLE credentials ADD COLUMN static_ip TEXT;          -- for proxy routing
ALTER TABLE credentials ADD COLUMN status TEXT DEFAULT 'pending'; -- pending, active, failed, max_retries_exceeded
ALTER TABLE credentials ADD COLUMN last_error TEXT;
ALTER TABLE credentials ADD COLUMN retry_count INTEGER DEFAULT 0;

-- Create indexes for deduplication lookups
CREATE INDEX IF NOT EXISTS idx_credentials_broker_client ON credentials(broker, client_id);
CREATE INDEX IF NOT EXISTS idx_credentials_user ON credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_status ON credentials(status);