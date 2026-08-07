-- Migration 043: DB-backed golden-repo alias lock table (Issue #1546 Phase 1).
--
-- Replaces the file-based WriteLockManager (Story #230, golden_repos_dir/
-- .locks/{alias}.lock) with a session-held-transaction lock: the lock IS
-- an open, uncommitted database transaction on a dedicated connection,
-- not a row with a TTL. See PostgresAliasLockStore
-- (server/services/alias_lock_store/postgres_store.py) for the acquire/
-- release/renew implementation.
--
-- Acquire: INSERT ... ON CONFLICT (lock_key) DO NOTHING RETURNING ...
-- Release: exact-token DELETE ... WHERE lock_key = %s AND owner_token = %s
--          on the SAME connection/transaction that acquired it. Zero rows
--          affected means ownership was already lost.
-- Renew:   exact-token UPDATE of last_renewed_at. Diagnostic-only -- NEVER
--          used for ownership decisions. No TTL, no reaper: a crashed
--          holder's connection death rolls its transaction back
--          automatically, freeing the lock immediately.
--
-- lock_key is the canonical key: bare alias (one trailing "-global"
-- suffix stripped, per the Bug #1373/#1390 convention) for golden-repo
-- operations, or an opaque string for non-golden keys (e.g. "cidx-meta").

CREATE TABLE IF NOT EXISTS golden_repo_alias_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL UNIQUE,
    operation        TEXT NOT NULL,
    acquired_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_renewed_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
