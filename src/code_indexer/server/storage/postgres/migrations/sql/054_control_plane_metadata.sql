-- Migration 054: control-plane metadata in the registry (content seam step 5a).
--
-- Golden-repo alias pointers (golden-repos/aliases/<alias>.json) and
-- write-mode markers (golden-repos/.write_mode/<alias>.json) are cidx's own
-- control-plane state, stored today as files on the repository volume that
-- phase 6 unmounts. They move here, next to global_repos and
-- golden_repos_metadata. One row per key; payload is the JSON document the
-- file used to hold, kept as TEXT (not JSONB) so the bytes replayed back to
-- the file during the rollback window are the bytes that were stored.
--
-- updated_at is a fixed-width UTC string (YYYY-MM-DDTHH:MM:SS.ffffffZ) so the
-- startup import's "never older over newer" upsert can compare it lexically.
--
-- Backward-compatible additive change only (CREATE TABLE IF NOT EXISTS).
-- The SQLite schema (DatabaseSchema in database_manager.py) creates the
-- same tables.

CREATE TABLE IF NOT EXISTS golden_repo_aliases (
    name        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS write_mode_markers (
    name        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
