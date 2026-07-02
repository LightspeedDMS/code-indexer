-- Migration 005: Add index on background_jobs.executing_node (Bug #544)
--
-- Several hot-path queries filter by executing_node: job release, completion,
-- failure, reconciliation sweeps. Without an index these require sequential scans.

CREATE INDEX IF NOT EXISTS idx_background_jobs_executing_node
    ON background_jobs (executing_node)
    WHERE executing_node IS NOT NULL;
