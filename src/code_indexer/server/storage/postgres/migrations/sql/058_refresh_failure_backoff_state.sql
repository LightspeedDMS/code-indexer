-- Migration 058: per-alias refresh failure backoff state (Bug #2022 Gap 4).
--
-- A golden-repo refresh that keeps failing for a reason the self-heal
-- cannot repair (disk full, permission denied, an unrepairable corrupt
-- chunks.db) must not be re-submitted every scheduling cycle or on every
-- trace-sync trigger. The consecutive-failure count and the wall-clock time
-- of the last failure are persisted here so every node and every submission
-- path sees the same exponential, capped backoff. A verified successful
-- refresh deletes the row.
--
-- last_failed_at is epoch seconds (time.time()), matching the SQLite
-- backend's REAL column, so the backoff window is computed identically on
-- both backends. Create-only: backward compatible with rolling restarts.

CREATE TABLE IF NOT EXISTS refresh_failure_backoff_state (
    golden_alias                TEXT PRIMARY KEY,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    last_detail                 TEXT,
    last_failed_at              DOUBLE PRECISION NOT NULL,
    updated_at                  TIMESTAMPTZ
);
