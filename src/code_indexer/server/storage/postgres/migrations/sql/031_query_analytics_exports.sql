-- Migration 031: Add query_analytics_exports table for export history tracking (Issue #1160)
--
-- Additive only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
-- No DROP/RENAME/TYPE-CHANGE (backward-compatible per CLAUDE.md rolling-upgrade safety rules).
--
-- The table stores one row per export job with filter metadata, file path,
-- and retention timestamp. Rows are pruned by QueryAnalyticsExportService.evict_old_exports()
-- based on export_retention_days (default 30 days).

CREATE TABLE IF NOT EXISTS query_analytics_exports (
    id              TEXT PRIMARY KEY,
    initiated_by    TEXT NOT NULL,
    created_at      DOUBLE PRECISION NOT NULL,
    status          TEXT NOT NULL,
    filter_summary  TEXT NOT NULL,
    file_path       TEXT,
    file_size_bytes INTEGER,
    row_count       INTEGER,
    error_message   TEXT,
    retention_until DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_qae_created_at
    ON query_analytics_exports (created_at);

CREATE INDEX IF NOT EXISTS idx_qae_initiated_by
    ON query_analytics_exports (initiated_by);
