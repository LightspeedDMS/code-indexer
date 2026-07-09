-- Migration 029: Add SHA-256 lookup column for API key bearer authentication (Bug #1144)
--
-- Additive only: ADD COLUMN, no DROP/RENAME/TYPE-CHANGE (backward-compatible per CLAUDE.md).
-- Existing rows get NULL key_sha256 — legacy keys will not authenticate via Bearer
-- (acceptable: the bearer path never worked before this migration).
--
-- The partial unique index enforces no two active keys share the same sha256.
-- WHERE key_sha256 IS NOT NULL avoids the index covering NULL rows (SQLite and PG both
-- support partial indexes; PG requires the WHERE clause to be a simple predicate).

ALTER TABLE user_api_keys ADD COLUMN key_sha256 TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_api_keys_sha256
    ON user_api_keys(key_sha256)
    WHERE key_sha256 IS NOT NULL;
