-- Migration 030: Add search_event_log table for per-query operational statistics (Issue #1159)
--
-- Additive only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
-- No DROP/RENAME/TYPE-CHANGE (backward-compatible per CLAUDE.md rolling-upgrade safety rules).
--
-- The table stores one row per search request with embedding cache telemetry.
-- Rows are pruned by SearchEventLogWriter._maybe_evict() based on
-- search_event_log_retention_days (default 90 days).

CREATE TABLE IF NOT EXISTS search_event_log (
    id                  BIGSERIAL PRIMARY KEY,
    timestamp           DOUBLE PRECISION NOT NULL,
    username            TEXT NOT NULL,
    repo_alias          TEXT,
    search_type         TEXT NOT NULL,
    query_text          TEXT NOT NULL,
    voyage_cache_hit    BOOLEAN,
    voyage_cache_mode   TEXT,
    voyage_latency_ms   INTEGER,
    cohere_cache_hit    BOOLEAN,
    cohere_cache_mode   TEXT,
    cohere_latency_ms   INTEGER,
    total_latency_ms    INTEGER NOT NULL,
    result_count        INTEGER NOT NULL,
    node_id             TEXT NOT NULL,
    correlation_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sel_timestamp
    ON search_event_log (timestamp);

CREATE INDEX IF NOT EXISTS idx_sel_user
    ON search_event_log (username);
