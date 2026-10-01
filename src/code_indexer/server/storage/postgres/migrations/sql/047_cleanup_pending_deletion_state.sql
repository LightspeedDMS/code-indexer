-- Migration 047: Bug #1567 -- durable pending-deletion queue for
-- versioned-snapshot cleanup.
--
-- CleanupManager (global_repos/cleanup_manager.py) used to hold its
-- pending-deletion queue ONLY in per-process dictionaries keyed by
-- time.monotonic(). Any process restart or uvicorn worker recycle inside
-- the 900-second minimum-retention-age window silently discarded the
-- scheduled deletion, and nothing ever reaped the orphaned snapshot
-- afterward -- confirmed live: 229 snapshots for one repo where policy
-- was keep-last-3, ~120GB leaked.
--
-- This table persists the queue in the shared backend (PostgreSQL here in
-- cluster mode, SQLite in solo mode -- see GoldenRepoMetadataSqliteBackend
-- for the mirror) so a scheduled deletion survives a restart. scheduled_at
-- is a WALL-CLOCK epoch-seconds value (Python time.time()), never
-- time.monotonic(), which has no meaning across process boundaries -- the
-- whole point is for the minimum-retention-age floor
-- (CleanupManager.MIN_RETENTION_AGE_SECONDS) to still be honored correctly
-- after a restart.
--
-- Keyed by index_path (one row per scheduled snapshot directory), never a
-- singleton row -- many snapshots can be pending deletion simultaneously.
-- Removed (DELETE) once the path is actually deleted or the per-path
-- circuit breaker trips (CleanupManager.MAX_FAILURES).

CREATE TABLE IF NOT EXISTS cleanup_pending_deletion_state (
    index_path      TEXT PRIMARY KEY,
    scheduled_at    DOUBLE PRECISION NOT NULL
);
