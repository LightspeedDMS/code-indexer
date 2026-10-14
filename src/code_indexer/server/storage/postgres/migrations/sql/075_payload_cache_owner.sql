-- Migration 075: creator of each cached payload entry.
--
-- owner  the username of the caller whose request created the entry.
--        The generic cached-content retrieval doors serve an entry only to
--        this user. NULL means no recorded creator (entries written before
--        this column existed, and key-addressed entries read only by the
--        route that wrote them); such entries are never served by the
--        generic doors.
--
-- payload_cache is created on first use by PayloadCachePostgresBackend,
-- which also adds this column in place, so the table may not exist yet when
-- migrations run: IF EXISTS keeps this a no-op then. Nullable add-column
-- only: backward compatible with rolling restarts (a node that predates the
-- column never reads or writes it).

ALTER TABLE IF EXISTS payload_cache
    ADD COLUMN IF NOT EXISTS owner TEXT;
