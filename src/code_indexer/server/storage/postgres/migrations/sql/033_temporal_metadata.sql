-- Migration 033: Add temporal_metadata table (Bug #1313)
--
-- Additive only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
-- No DROP/RENAME/TYPE-CHANGE (backward-compatible per CLAUDE.md rolling-upgrade
-- safety rules).
--
-- Root cause (Bug #1313): TemporalMetadataStore (Story #669) was a SQLite-WAL
-- database that, in cluster mode, lives on the shared NFS golden-repos mount.
-- NFS cannot satisfy SQLite WAL's -shm requirement, serializing all 8 indexing
-- threads on the same fsync. This table backs TemporalMetadataPostgresBackend,
-- which replaces ONLY the storage engine (schema/operations identical to the
-- SQLite backend) with PostgreSQL.
--
-- Unlike SQLite (one .db file per collection), this ONE table holds every
-- collection's rows -- collection_key (a sha256 prefix of the collection path,
-- computed by TemporalMetadataStore) scopes all reads/writes to a single
-- logical temporal collection. hash_prefix is the 16-char sha256(point_id)
-- prefix used as the vector filename (vector_<hash_prefix>.json).

CREATE TABLE IF NOT EXISTS temporal_metadata (
    collection_key  TEXT NOT NULL,
    hash_prefix     TEXT NOT NULL,
    point_id        TEXT NOT NULL,
    commit_hash     TEXT,
    file_path       TEXT,
    chunk_index     INTEGER,
    created_at      TEXT,
    format_version  INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY (collection_key, hash_prefix)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_temporal_meta_pointid
    ON temporal_metadata (collection_key, point_id);

CREATE INDEX IF NOT EXISTS idx_temporal_meta_commit
    ON temporal_metadata (collection_key, commit_hash);
