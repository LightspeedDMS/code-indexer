-- Migration 037: Golden-repo registry-reconcile auto-heal event trace
-- (GitHub Issue #1383, follow-up to Bug #1382).
--
-- Bug #1382 added a persisted, cross-restart circuit-breaker confirmation
-- counter (golden_repo_reconcile_breaker_state) so a genuinely persistent
-- orphan set eventually auto-heals after CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD
-- confirmations. Issue #1383 found that the moment auto-removal actually
-- fires, the breaker-state counter is reset in the SAME tick -- so the
-- /health DEGRADED signal disappears exactly when the irreversible
-- mass-deletion happens, leaving no persistent trace beyond a log line.
--
-- This singleton-row (id=1) table records the most recent confirmed
-- auto-removal event (which aliases were actually removed, and when) so
-- it remains independently discoverable -- via the
-- last_golden_repo_reconcile_auto_heal field on the /health REST response
-- (HealthCheckResponse, populated in HealthCheckService.get_system_health()
-- via get_golden_repo_reconcile_auto_heal_event()) -- even after
-- golden_repo_reconciler.py's reset_reconcile_breaker_state() clears the
-- confirmation counter. See golden_repo_reconciler.py and the SQLite
-- counterpart in sqlite_backends.py (GoldenRepoMetadataSqliteBackend) for
-- the read/write contract.
--
-- This is informational only -- it must NEVER be folded into the
-- DEGRADED failure_reasons surface (that surface reports CURRENT,
-- unresolved conditions; this table records a historical event that has
-- already been resolved by the auto-removal itself).

CREATE TABLE IF NOT EXISTS golden_repo_reconcile_auto_heal_event (
    id                  INTEGER PRIMARY KEY,
    removed_aliases     TEXT NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL
);
