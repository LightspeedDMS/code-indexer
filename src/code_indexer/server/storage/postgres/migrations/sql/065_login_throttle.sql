-- Migration 065: progressive per-username login throttle.
--
-- Replaces the hard account lockout (login_failures / login_lockouts, kept
-- in place and no longer written). One row per throttle key with an attempt
-- within the window: the key is the SHA-256 of scope + NUL + subject, with
-- scope 'login' (the typed username) or 'stepup' (the authenticated
-- username of a TOTP step-up) -- never the text. failure_count counts
-- consecutive reserved attempts, and blocked_until is the end of the
-- running backoff window (0 when none). Rows idle longer than the window
-- are pruned through the last_failure_at index whenever a new row is
-- inserted. Create-only: backward compatible with rolling restarts.

CREATE TABLE IF NOT EXISTS login_throttle (
    key_hash        TEXT PRIMARY KEY,
    failure_count   INTEGER NOT NULL,
    last_failure_at DOUBLE PRECISION NOT NULL,
    blocked_until   DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_throttle_last_failure
    ON login_throttle(last_failure_at);
