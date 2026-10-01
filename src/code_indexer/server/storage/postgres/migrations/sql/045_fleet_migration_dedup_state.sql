-- Migration 045: Story #1560 -- per-repo duplicate-point-id auto-
-- resolution outcome state.
--
-- Fleet migration (Story #1458) previously FAILED CLOSED when a legacy
-- SHARDED_JSON collection contained a duplicate point_id id_index.bin
-- could not arbitrate: it raised DuplicateSourceIdError, left the
-- collection untouched, and quarantined the repo after 3 consecutive
-- failures (fleet_migration_quarantine_state). Story #1560 replaces
-- that: migration always proceeds, duplicates are resolved automatically
-- and DELETED (no sidecar, no leftovers), and the occurrence is recorded
-- here so the operator sees it on /health and decides whether to
-- re-index.
--
-- Deliberately a NEW table, not a reuse of fleet_migration_quarantine_
-- state -- that table means "this repo keeps failing"; this one means
-- "this repo migrated successfully but permanently lost N records".
-- Consistent with golden_repo_reconcile_breaker_state /
-- golden_repo_reconcile_auto_heal_event / hnsw_orphan_sweep_state, each
-- of which is its own dedicated table for its own distinct condition.
--
-- Cumulative per golden_alias (AC9): repeated repair passes ADD to the
-- existing row's duplicate_groups/records_deleted/winner_kept_groups/
-- whole_group_deleted_groups counters (the deletions from a PRIOR pass
-- are permanent and must never be double-counted or discarded by a
-- later pass; an already-clean collection's repeat pass records nothing
-- new, per AC10). records_before/collection_total are the FILE-SCAN-
-- basis measurement from the MOST RECENT pass (see
-- collection_dedup_repair.py's DedupRepairResult docstring for why this
-- basis can differ from id_index.bin's own entry count on real data).
--
-- Cleared (AC8) only via an explicit clear_dedup_state() call, tied by
-- the caller to a successful full re-index's completion marker/
-- generation -- never by an ordinary migration retry, and never
-- time-based.
--
-- golden_alias is stored in its NORMALIZED (bare, no "-global" suffix)
-- form (AC18) -- callers must normalize before every read/write.

CREATE TABLE IF NOT EXISTS fleet_migration_dedup_state (
    golden_alias                TEXT PRIMARY KEY,
    duplicate_groups            INTEGER NOT NULL DEFAULT 0,
    records_before               INTEGER NOT NULL DEFAULT 0,
    records_deleted              INTEGER NOT NULL DEFAULT 0,
    winner_kept_groups           INTEGER NOT NULL DEFAULT 0,
    whole_group_deleted_groups   INTEGER NOT NULL DEFAULT 0,
    collection_total             INTEGER NOT NULL DEFAULT 0,
    first_dropped_at             TIMESTAMPTZ,
    dropped_at                   TIMESTAMPTZ,
    cleared_at                   TIMESTAMPTZ,
    cleared_reason               TEXT
);
