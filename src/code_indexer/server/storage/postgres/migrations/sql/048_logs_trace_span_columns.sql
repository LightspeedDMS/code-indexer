-- Story #1676 AC2: Add `trace_id`/`span_id` columns to `logs` so operators
-- can jump from a stored log row directly to the OTEL trace that produced
-- it, in both SQLite (solo) and PostgreSQL (cluster) storage modes.
--
-- Backward-compatible additive change:
--   - CREATE TABLE IF NOT EXISTS            (no-op if already present)
--   - ALTER TABLE ADD COLUMN IF NOT EXISTS  (no-op if already present)
--
-- Old code that writes log rows without trace_id/span_id continues to
-- work; the documented zero-values ("0"*32 / "0"*16) are always populated
-- by the production wiring point (logging_utils.inject_trace_context, via
-- async_logging.IdentityQueueHandler.prepare()) before a row is written,
-- so these columns are NEVER NULL for a row written by an AC2-aware
-- server -- NULL only appears for pre-existing rows written before this
-- migration ran.
--
-- The `logs` table is normally created at server startup by LogsBackend
-- (_ensure_schema). On fresh cluster installs the migration runner executes
-- BEFORE the server starts, so we must create it here if it does not exist
-- (mirrors migration 020_logs_alias_column.sql's rationale exactly).

CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT,
    message TEXT,
    correlation_id TEXT,
    user_id TEXT,
    request_path TEXT,
    extra_data TEXT,
    node_id TEXT,
    alias TEXT,
    trace_id TEXT,
    span_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE logs
    ADD COLUMN IF NOT EXISTS trace_id TEXT;

ALTER TABLE logs
    ADD COLUMN IF NOT EXISTS span_id TEXT;
