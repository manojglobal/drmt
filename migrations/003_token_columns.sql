-- 003_token_columns.sql
-- Add 'token' and 'token_updated_at' columns used by the Python dedup engine.
-- The existing 002 migration added 'access_token' / 'access_token_created_at' (Go names),
-- but the Python implementation uses 'token' / 'token_updated_at' consistently.
-- Both columns are safe to ADD even on fresh databases (try/except in db.py handles duplicates).

ALTER TABLE credentials ADD COLUMN token TEXT DEFAULT '';
ALTER TABLE credentials ADD COLUMN token_updated_at DATETIME;
