-- Migration 055: the control-plane metadata dual-write outbox (content seam 5a).
--
-- During the rollback window every alias / write-mode marker change in the
-- registry (migration 054) is also written to the old file. The registry
-- change and an outbox row commit in ONE transaction; a sweeper on the leader
-- replays the outbox to the files and deletes a row only once its write
-- succeeded, so a failed file write delays the file but never drops it.
--
-- A row names a KEY (kind + name), never a payload: the sweeper writes the
-- registry's CURRENT value for the key (or deletes the file when the key is
-- gone) and drops every row for the key up to the one it served, so a failed
-- older row retried after a newer one never writes a stale value. Claimed
-- with FOR UPDATE SKIP LOCKED.
--
-- metadata_outbox_state is a singleton (id = 1): the drain clock and the
-- stop flag (drain bound reached, or an operator abandon), shared by every
-- node so an abandon on any node stops the leader's sweeper.
--
-- Rollback gate: SELECT count(*) FROM metadata_outbox returns 0.
--
-- Backward-compatible additive change only. The SQLite schema
-- (DatabaseSchema in database_manager.py) creates the same tables, plus the
-- claim lease columns SQLite needs in place of row locks.

CREATE TABLE IF NOT EXISTS metadata_outbox (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metadata_outbox_key
    ON metadata_outbox (kind, name, id);

CREATE TABLE IF NOT EXISTS metadata_outbox_state (
    id                   INTEGER PRIMARY KEY,
    drain_started_epoch  DOUBLE PRECISION,
    drain_sink           TEXT,
    stopped_at           TEXT,
    stopped_reason       TEXT,
    stopped_sink         TEXT
);
