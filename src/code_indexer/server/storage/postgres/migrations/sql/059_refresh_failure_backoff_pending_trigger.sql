-- Migration 059: durable deferred refresh trigger (Bug #2022 Gap 4).
--
-- A system refresh trigger (e.g. a Langfuse trace write) that arrives while
-- the alias is inside its persisted failure backoff is deferred, not
-- dropped: pending_trigger marks it, and the refresh scheduler on any node
-- atomically claims and submits it once the backoff ends. The flag lives on
-- the alias's backoff row, so a verified successful refresh (which deletes
-- the row) also clears it.
--
-- Add-column only with a default: backward compatible with rolling restarts.

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS pending_trigger BOOLEAN NOT NULL DEFAULT FALSE;
