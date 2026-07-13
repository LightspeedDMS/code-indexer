-- Migration 036: Golden-repo registry-reconcile circuit-breaker confirmation
-- state (Bug #1382).
--
-- Bug #1317's registry-reconcile sweep (golden_repo_reconciler.py) refuses
-- to delete anything when more than half of registered golden repos
-- resolve absent (ORPHAN_FRACTION_ABORT_THRESHOLD), on the theory that such
-- a high ratio usually means an infra/mount problem rather than real
-- orphans. Bug #1382 found a live staging incident where this protection
-- had no path to resolution: 8/14 (57%) repos were genuine, persistent
-- registry-orphans (crash-recovery gap), and the circuit-breaker aborted on
-- every single restart for ~2 months.
--
-- This singleton-row (id=1) table lets the sweep distinguish "the SAME
-- orphan-candidate set observed on multiple CONSECUTIVE sweeps, each with a
-- healthy base directory" (real orphans -- eventually auto-heals after
-- CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD confirmations) from a genuine
-- one-off blip or real infra flapping (which resets this state and must
-- keep aborting forever). See golden_repo_reconciler.py and the SQLite
-- counterpart in sqlite_backends.py (GoldenRepoMetadataSqliteBackend) for
-- the read/write contract.

CREATE TABLE IF NOT EXISTS golden_repo_reconcile_breaker_state (
    id                  INTEGER PRIMARY KEY,
    orphan_fingerprint  TEXT,
    consecutive_count   INTEGER NOT NULL DEFAULT 0,
    first_observed_at   TIMESTAMPTZ,
    last_observed_at    TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ
);
