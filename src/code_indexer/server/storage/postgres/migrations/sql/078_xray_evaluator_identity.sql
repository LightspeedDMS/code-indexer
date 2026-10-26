-- Migration 078: which X-Ray evaluator code ran (Story #2203).
--
-- search_event_log.evaluator_sha256   SHA-256 of the evaluator source that
--                                     ran (a batch row lists its scans'
--                                     hashes, comma-separated)
-- search_event_log.xray_pattern_name  the stored pattern name(s) used
-- xray_evaluator_sources              each distinct evaluator source once,
--                                     keyed by its SHA-256; source is capped
--                                     (source_truncated marks a cut source)
--
-- Rows written before these columns existed read NULL. A table and nullable
-- columns only: backward compatible with rolling restarts (a node that
-- predates them never reads or writes them). The search event writer also
-- creates them (SearchEventLogPostgresBackend._ensure_schema).

ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS evaluator_sha256 TEXT;
ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS xray_pattern_name TEXT;

CREATE TABLE IF NOT EXISTS xray_evaluator_sources (
    evaluator_sha256 TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    source_truncated BOOLEAN NOT NULL,
    first_seen       DOUBLE PRECISION NOT NULL,
    last_seen        DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_xes_last_seen
    ON xray_evaluator_sources (last_seen);
