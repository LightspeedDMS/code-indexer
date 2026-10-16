-- Migration 064: per-alias forced-reconcile state.
--
-- The refresh scheduler forces a reconcile when a golden repo's index
-- metadata shows a stale signal (interrupted run, drifted commit) although
-- git reports no new commits. When a forced reconcile completes and the SAME
-- signal is still present, forcing again changes nothing; after a small
-- bound the scheduler stops forcing until the signal changes. The signal
-- text and the number of consecutive forced reconciles that left it
-- unchanged are persisted here so the bound survives restarts and is seen
-- by every node. Create-only: backward compatible with rolling restarts.

CREATE TABLE IF NOT EXISTS forced_reconcile_state (
    golden_alias    TEXT PRIMARY KEY,
    signal          TEXT NOT NULL,
    attempt_count   INTEGER NOT NULL,
    updated_at      TIMESTAMPTZ
);
