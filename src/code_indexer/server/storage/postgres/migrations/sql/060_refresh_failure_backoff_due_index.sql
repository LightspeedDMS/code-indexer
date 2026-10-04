-- Migration 060: due time for deferred refresh triggers (Bug #2022 Gap 4).
--
-- A deferred system refresh trigger (pending_trigger, migration 059) is due
-- at pending_due_at. A scheduler LEASES a due trigger by moving
-- pending_due_at forward, without clearing pending_trigger, so a process
-- death before the refresh completes only delays the trigger to the lease
-- end; only a verified publish resolves it. pending_marked_at is when the
-- trigger was last deferred: a trigger deferred during a refresh cycle may
-- not be covered by that cycle's publish, so the publish keeps it.
--
-- The partial index serves the scheduler's due query, which touches only
-- due triggers rather than every failing alias.
--
-- Add-column, backfill and create-index only: backward compatible with
-- rolling restarts. A trigger recorded before this migration is due at once.

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS pending_due_at DOUBLE PRECISION;

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS pending_marked_at DOUBLE PRECISION;

UPDATE refresh_failure_backoff_state
    SET pending_due_at = last_failed_at
    WHERE pending_trigger AND pending_due_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_refresh_failure_backoff_due
    ON refresh_failure_backoff_state (pending_due_at)
    WHERE pending_trigger;
