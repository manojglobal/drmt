-- 005_axis_kotak_credentials.sql
-- Add broker-specific columns for Kotak Neo and Axis Direct integrations.
-- Columns are safe to ADD even if already present (try/except in db.py handles duplicate column errors).

ALTER TABLE credentials ADD COLUMN sid TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN base_url TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN sub_account_id TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN public_key TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN refresh_token TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN mobile_number TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN mpin TEXT DEFAULT '';
