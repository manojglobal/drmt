-- 004_auth_audit_logs.sql
CREATE TABLE IF NOT EXISTS auth_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    details TEXT,
    timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON auth_audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user ON auth_audit_logs(username);
CREATE INDEX IF NOT EXISTS idx_audit_action ON auth_audit_logs(action);
