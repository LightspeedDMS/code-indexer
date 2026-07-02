-- Migration 009: Login rate limiting tables for cluster-mode account lockout.
-- Story #557 / H1: Shared failure/lockout state across cluster nodes.
-- Backward compatible: new tables only, no modifications to existing schema.

CREATE TABLE IF NOT EXISTS login_failures (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL,
    failed_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_failures_user_time
ON login_failures(username, failed_at);

CREATE TABLE IF NOT EXISTS login_lockouts (
    username TEXT PRIMARY KEY,
    locked_until DOUBLE PRECISION NOT NULL
);
