-- Bug #874 Story B: Create dependency_map_run_history table for PostgreSQL.
--
-- LATENT DEFECT FIX: This table existed only in the SQLite backend init path.
-- A fresh PostgreSQL cluster deploy crashes on the first record_run_metrics()
-- call because no migration created this table.  This migration fixes that.
--
-- All changes are idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS)
-- so running this against an out-of-band-created table is safe.
--
-- Bug #874 Story B also adds run_type and phase_timings_json:
--   run_type          VARCHAR(16) -- NULL for legacy rows (pre-Story-B)
--   phase_timings_json JSONB      -- NULL for legacy rows; JSONB per project
--                                    convention (see 001_initial_schema.sql:273
--                                    dependency_map_tracking.commit_hashes pattern
--                                    and Q3 resolution in bug body).

CREATE TABLE IF NOT EXISTS dependency_map_run_history (
    run_id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    domain_count INTEGER NOT NULL DEFAULT 0,
    total_chars INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    zero_char_domains INTEGER NOT NULL DEFAULT 0,
    repos_analyzed INTEGER NOT NULL DEFAULT 0,
    repos_skipped INTEGER NOT NULL DEFAULT 0,
    pass1_duration_s REAL NOT NULL DEFAULT 0.0,
    pass2_duration_s REAL NOT NULL DEFAULT 0.0,
    run_type VARCHAR(16),
    phase_timings_json JSONB
);

-- Idempotent column additions for installations where the table was created
-- out-of-band (e.g. via SQLite-parity scripts) without the new columns.
ALTER TABLE dependency_map_run_history
    ADD COLUMN IF NOT EXISTS run_type VARCHAR(16);

ALTER TABLE dependency_map_run_history
    ADD COLUMN IF NOT EXISTS phase_timings_json JSONB;
