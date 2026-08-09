-- Migration 044: cidx-meta backup conflict-resolution per-repo failure
-- quarantine state (Bug #1539).
--
-- CidxMetaBackupSync.sync() (server/services/cidx_meta_backup/sync.py)
-- rebases local cidx-meta writes onto origin/{branch}; when the rebase
-- conflicts and the Claude-CLI conflict resolver cannot resolve it, this
-- table persists a consecutive-failure counter keyed on the UPSTREAM
-- TARGET COMMIT SHA being rebased onto (never freeform git/LLM error
-- text -- an earlier text-fingerprint design was rejected on review:
-- proven fragile both ways, with genuinely different failures
-- collapsing to the same text shape AND the SAME failure's varying
-- rebase-position text failing to match across attempts). A commit SHA
-- has no such ambiguity. Persisted here so the count is visible across
-- the multi-worker/multi-node process boundary scheduled cidx-meta
-- refresh actually runs in -- an in-process counter would restart at
-- zero on every different worker/node and never trip.
--
-- RefreshScheduler resolves the CURRENT upstream target SHA and checks
-- this state BEFORE calling sync() again
-- (_cidx_meta_conflict_quarantine_skip_result) -- skipping the sync
-- attempt entirely once the SAME target SHA has failed consecutively
-- enough times, stopping the endless pile of FAILED global_repo_refresh
-- jobs a structurally unresolvable conflict would otherwise produce on
-- every scheduled tick. Critically, this ALSO self-heals automatically:
-- the moment new commits land on the upstream branch (or its history
-- changes), the resolved target SHA differs from the stored one, the
-- skip check no longer matches, and a fresh attempt proceeds -- no
-- manual quarantine reset is ever required for a genuinely resolved
-- situation.
--
-- Distinct from refresh_integrity_quarantine_state (migration 041): that
-- table always increments on any failure (no keying concept, since
-- refresh naturally alternates try/reset). This table's increment is
-- target-SHA-CONDITIONAL: a different upstream target resets the count
-- to 1 instead of inheriting an unrelated prior tally.

CREATE TABLE IF NOT EXISTS cidx_meta_conflict_quarantine_state (
    golden_alias                TEXT PRIMARY KEY,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    last_target_sha             TEXT,
    last_detail                 TEXT,
    first_failed_at             TIMESTAMPTZ,
    last_failed_at              TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ
);
