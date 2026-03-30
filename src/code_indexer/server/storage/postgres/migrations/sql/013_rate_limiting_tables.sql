-- Migration 013: Generic rate limiting tables for cluster mode.
-- Bug #573/#574: All rate limiters (password_change, refresh_token,
-- oauth_token, oauth_register) share these tables, differentiated
-- by limiter_type column.
-- Backward compatible: new tables only, no modifications to existing schema.

CREATE TABLE IF NOT EXISTS rate_limit_failures (
    id SERIAL PRIMARY KEY,
    limiter_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    failed_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_failures_lookup
    ON rate_limit_failures(limiter_type, identifier, failed_at);

CREATE TABLE IF NOT EXISTS rate_limit_lockouts (
    id SERIAL PRIMARY KEY,
    limiter_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    locked_until DOUBLE PRECISION NOT NULL,
    UNIQUE(limiter_type, identifier)
);
