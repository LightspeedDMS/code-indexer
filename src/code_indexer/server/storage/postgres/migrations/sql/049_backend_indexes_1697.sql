-- Migration 049: Close index-coverage gaps left behind by Issue #1697's
-- dead-`_ensure_schema()` sweep.
--
-- Issue #1697 deletes several PostgreSQL backends' dead `_ensure_schema()`
-- self-heal blocks (CREATE TABLE IF NOT EXISTS statements that never
-- actually run in production, since MigrationRunner always runs before
-- any backend is constructed -- see the Bug #1655/#1662 precedent). Two
-- of those dead blocks also created indexes that no migration ever
-- mirrored:
--
--   - RefreshTokenPostgresBackend._ensure_schema() created
--     idx_token_expires ON refresh_tokens (expires_at), used by
--     delete_expired_tokens()'s WHERE expires_at < %s cleanup query.
--   - SelfMonitoringPostgresBackend._ensure_schema() created
--     idx_sm_scans_started_at ON self_monitoring_scans (started_at) and
--     idx_sm_issues_created_at ON self_monitoring_issues (created_at),
--     used by the scan-history/fingerprint ORDER BY / WHERE queries.
--
-- Deleting the backend code without also adding these indexes here would
-- leave a brand-new cluster install (post-fix) permanently missing them,
-- since existing deployments already created them on disk via a prior
-- server boot. All statements are idempotent (CREATE INDEX IF NOT
-- EXISTS) and additive-only.

CREATE INDEX IF NOT EXISTS idx_token_expires
    ON refresh_tokens (expires_at);

CREATE INDEX IF NOT EXISTS idx_sm_scans_started_at
    ON self_monitoring_scans (started_at);

CREATE INDEX IF NOT EXISTS idx_sm_issues_created_at
    ON self_monitoring_issues (created_at);
