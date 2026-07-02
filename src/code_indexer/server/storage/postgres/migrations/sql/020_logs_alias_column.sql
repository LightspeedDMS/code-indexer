-- Story #876 Phase C: Add `alias` column to `logs` so the lifecycle-runner
-- can tag ERROR rows with the repo they came from.  The admin UI uses this
-- to filter logs by repo when diagnosing failed lifecycle generations.
--
-- Backward-compatible additive change:
--   - ALTER TABLE ADD COLUMN IF NOT EXISTS  (no-op if already present)
--   - CREATE INDEX IF NOT EXISTS            (no-op if already present)
--
-- Old code that writes log rows without `alias` continues to work; the
-- column simply stays NULL for those rows.
--
-- Cluster-mode fix: the `logs` table is otherwise created LAZILY by
-- LogsPostgresBackend._ensure_schema(), but in postgres/cluster mode all
-- migrations run (service_init.py) BEFORE the storage backends are created —
-- so on a fresh cluster database this ALTER would fail fail-fast with
-- 'relation "logs" does not exist'. Create the base table here (IF NOT EXISTS,
-- so it's a no-op on existing/standalone databases where the backend already
-- made it). Schema mirrors LogsPostgresBackend so the later lazy CREATE is a no-op.

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
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE logs
    ADD COLUMN IF NOT EXISTS alias TEXT;

CREATE INDEX IF NOT EXISTS idx_logs_pg_alias
    ON logs(alias);
