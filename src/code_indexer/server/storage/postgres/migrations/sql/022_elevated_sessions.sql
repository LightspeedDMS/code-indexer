-- Story #923 AC2: Create elevated_sessions table for PostgreSQL cluster mode.
--
-- Tracks TOTP step-up elevation windows.  Mirrors the SQLite schema
-- initialised by ElevatedSessionManager._ensure_schema() so that both
-- solo (SQLite) and cluster (PostgreSQL) nodes share the same structure.
--
-- All changes are idempotent (CREATE TABLE IF NOT EXISTS,
-- CREATE INDEX IF NOT EXISTS) so applying this migration against a
-- database where the table was created out-of-band is safe.

CREATE TABLE IF NOT EXISTS elevated_sessions (
    session_key TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    elevated_at DOUBLE PRECISION NOT NULL,
    last_touched_at DOUBLE PRECISION NOT NULL,
    elevated_from_ip TEXT,
    scope TEXT NOT NULL DEFAULT 'full'
);

CREATE INDEX IF NOT EXISTS idx_elevated_sessions_last_touched
    ON elevated_sessions(last_touched_at);

CREATE INDEX IF NOT EXISTS idx_elevated_sessions_username
    ON elevated_sessions(username);
