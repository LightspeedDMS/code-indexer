-- Migration 040: Fleet-migration per-repo failure quarantine state
-- (Issue #1477).
--
-- FleetMigrationScheduler._run_next_candidate() always picks the FIRST
-- not-yet-migrated golden repo (alias-sorted order) with no memory of
-- prior attempts. A repo whose migration throws every single time (e.g.
-- genuinely corrupt legacy vector_*.json data that scan_vectors_for_id_map
-- correctly refuses to auto-resolve) was therefore retried forever,
-- permanently starving every alphabetically-later repo in the fleet.
--
-- This table persists a consecutive-failure counter per golden_alias
-- (unlike golden_repo_reconcile_breaker_state's singleton row, this is
-- keyed per-alias since many repos are tracked independently). Once a
-- repo's consecutive_failure_count reaches the scheduler's quarantine
-- threshold, it is skipped so the fleet-wide migration queue can advance.
--
-- state_signature is a cheap, BOUNDED 4-level-recursive fingerprint of
-- the repo's on-disk collection/temporal directory state (see
-- quarantine.py's _SIGNATURE_MAX_SHARD_DEPTH / _collect_dir_state_tokens
-- -- never an unbounded walk, and never O(files in the whole
-- collection)), recorded at the time of the MOST RECENT failure. A
-- quarantine auto-clears ONLY when the CURRENT on-disk signature differs
-- from this stored value -- mirroring description_refresh_scheduler.py's
-- PROMPT_FAILURE_QUARANTINE_THRESHOLD commit-based auto-clear gate (Bug
-- #1096): a bare retry must never look like evidence of a genuine
-- change.

CREATE TABLE IF NOT EXISTS fleet_migration_quarantine_state (
    golden_alias                TEXT PRIMARY KEY,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    state_signature              TEXT,
    first_failed_at              TIMESTAMPTZ,
    last_failed_at               TIMESTAMPTZ,
    updated_at                   TIMESTAMPTZ,
    signature_checked_at         TIMESTAMPTZ,
    failure_cause                TEXT
);
