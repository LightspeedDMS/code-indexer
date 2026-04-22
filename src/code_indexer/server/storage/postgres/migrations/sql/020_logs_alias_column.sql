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

ALTER TABLE logs
    ADD COLUMN IF NOT EXISTS alias TEXT;

CREATE INDEX IF NOT EXISTS idx_logs_pg_alias
    ON logs(alias);
