-- Migration 044: cidx-meta backup conflict-resolution per-repo failure
-- quarantine state (Bug #1539).
--
-- CidxMetaBackupSync.sync() (server/services/cidx_meta_backup/sync.py)
-- rebases local cidx-meta writes onto origin/{branch}; when the rebase
-- conflicts and the Claude-CLI conflict resolver cannot resolve it, this
-- table persists a consecutive-failure counter (PLUS the last failure's
-- normalized fingerprint) per golden_alias so RepeatedFailureGuard-style
-- classification survives across the multi-worker/multi-node process
-- boundary that scheduled cidx-meta refresh actually runs in -- an
-- in-process counter would restart at zero on every different
-- worker/node and never trip.
--
-- RefreshScheduler checks this state BEFORE calling sync() again
-- (_cidx_meta_conflict_quarantine_skip_result) and skips the sync
-- attempt entirely once the same fingerprint has failed
-- consecutively enough times -- stopping the endless pile of FAILED
-- global_repo_refresh jobs a structurally unresolvable conflict would
-- otherwise produce on every scheduled tick.
--
-- Distinct from refresh_integrity_quarantine_state (migration 041): that
-- table always increments on any failure (no fingerprint concept, since
-- refresh naturally alternates try/reset). This table's increment is
-- fingerprint-CONDITIONAL: a genuinely different failure shape resets
-- the count to 1 instead of inheriting an unrelated prior tally.

CREATE TABLE IF NOT EXISTS cidx_meta_conflict_quarantine_state (
    golden_alias                TEXT PRIMARY KEY,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    last_fingerprint            TEXT,
    last_detail                 TEXT,
    first_failed_at             TIMESTAMPTZ,
    last_failed_at              TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ
);
