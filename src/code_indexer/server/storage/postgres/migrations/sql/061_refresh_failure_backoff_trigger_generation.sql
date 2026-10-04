-- Migration 061: store-ordered generation for deferred refresh triggers
-- (Bug #2022 Gap 4).
--
-- Every deferral of a system refresh trigger advances trigger_generation in
-- the same UPDATE that marks the trigger pending. A refresh cycle captures
-- the generation from the store before it reads its source, and a verified
-- publish deletes the row only while the generation is still the captured
-- one; a trigger deferred during the cycle advanced it, so the row survives
-- re-armed. This replaces a comparison of wall clocks taken on different
-- nodes, which clock skew could invert. pending_marked_at (migration 060)
-- is informational only.
--
-- Add-column only with a default: backward compatible with rolling restarts.

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS trigger_generation INTEGER NOT NULL DEFAULT 0;
