-- Migration 050: Local-repo `cidx init` repair per-repo failure
-- quarantine state (Bug #1769).
--
-- RefreshScheduler._execute_refresh() self-heals a local repo whose
-- .code-indexer/ directory exists but has no valid config.json (Bug
-- #1253) by re-running `cidx init --force`. Before this table existed,
-- the repair attempt had NO persisted failure state at all -- a
-- permanently-broken local repo (e.g. an auto-discovery-created
-- langfuse_Claude_Code_*-global repo) re-ran the repair AND logged an
-- ERROR on every single scheduled refresh cycle, forever, with zero
-- convergence. Observed as 1,151 recurring "Failed to repair
-- uninitialized local repo ... via 'cidx init'" log entries over 3+ days
-- across multiple deploys on staging, with zero progress.
--
-- This table persists a consecutive-failure counter per golden_alias so
-- REPEATED failures can be surfaced to an operator (a single loud ERROR
-- log at the confirmation threshold) instead of retrying identically
-- forever, mirroring migration 041's refresh_integrity_quarantine_state
-- (Bug #1506) -- the structurally identical "ordinary [repair] naturally
-- alternates try/reset each scheduled cycle" domain, where a bare
-- consecutive counter (no content-signature auto-clear) is sufficient.

CREATE TABLE IF NOT EXISTS local_repo_repair_quarantine_state (
    golden_alias                TEXT PRIMARY KEY,
    consecutive_failure_count   INTEGER NOT NULL DEFAULT 0,
    last_detail                 TEXT,
    first_failed_at             TIMESTAMPTZ,
    last_failed_at              TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ
);
