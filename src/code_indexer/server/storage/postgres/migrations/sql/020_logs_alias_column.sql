-- Story #876 Phase C: Add `alias` column to `logs` so the lifecycle-runner
-- can tag ERROR rows with the repo they came from.  The admin UI uses this
-- to filter logs by repo when diagnosing failed lifecycle generations.
--
-- Backward-compatible additive change:
--   - CREATE TABLE IF NOT EXISTS            (no-op if already present)
--   - ALTER TABLE ADD COLUMN IF NOT EXISTS  (no-op if already present)
--   - CREATE INDEX IF NOT EXISTS            (no-op if already present)
--
-- Old code that writes log rows without `alias` continues to work; the
-- column simply stays NULL for those rows.
--
-- The `logs` table is normally created at server startup by LogsBackend
-- (_ensure_schema). On fresh cluster installs the migration runner executes
-- BEFORE the server starts, so we must create it here if it does not exist.

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
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logs_pg_timestamp ON logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_pg_level ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_pg_node_id ON logs(node_id);
CREATE INDEX IF NOT EXISTS idx_logs_pg_correlation_id ON logs(correlation_id);

ALTER TABLE logs
    ADD COLUMN IF NOT EXISTS alias TEXT;

CREATE INDEX IF NOT EXISTS idx_logs_pg_alias
    ON logs(alias);
