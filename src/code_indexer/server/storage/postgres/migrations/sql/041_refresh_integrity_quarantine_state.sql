-- Migration 041: Ordinary-refresh integrity-gate per-repo failure
-- quarantine state (Bug #1506).
--
-- RefreshScheduler._execute_refresh() runs a durability-flush +
-- PRAGMA integrity_check gate against the just-mutated chunks.db before
-- publishing a snapshot. On failure, the current cycle self-heals via a
-- reflink restore from the last-known-good snapshot and skips publish.
-- This table persists a consecutive-failure counter per golden_alias so
-- REPEATED failures (the self-heal itself keeps failing, or the source
-- keeps corrupting) can be surfaced to an operator instead of retrying
-- silently forever.
--
-- Deliberately simpler than fleet_migration_quarantine_state (migration
-- 040): unlike fleet migration (which retries the SAME repo every
-- scheduler tick regardless of outcome, needing a content-signature to
-- distinguish "genuine on-disk change" from "bare retry"), ordinary
-- refresh naturally alternates try/reset each scheduled cycle -- a bare
-- consecutive counter (increment on failure, reset to zero on any
-- success) is sufficient.

CREATE TABLE IF NOT EXISTS refresh_integrity_quarantine_state (
    golden_alias                TEXT PRIMARY KEY,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    last_detail                 TEXT,
    first_failed_at             TIMESTAMPTZ,
    last_failed_at              TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ
);
