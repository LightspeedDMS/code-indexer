-- Migration 077: the golden-repo alias lock becomes a committed, row-owned lease.
--
-- The row (not the session that inserted it) owns the lock, so any worker can
-- release it, and a holder that stops renewing is stale once
-- now() - last_renewed_at exceeds ttl_seconds (or, on its own host, once its
-- process is gone). See server/services/alias_lock_store/base.py.
--
-- Nullable, additive columns only: a node still on the previous release
-- inserts (lock_key, owner_token, operation) and keeps working during a
-- rolling upgrade; NULL ttl_seconds reads as the default lease.

ALTER TABLE golden_repo_alias_locks ADD COLUMN IF NOT EXISTS hostname TEXT;
ALTER TABLE golden_repo_alias_locks ADD COLUMN IF NOT EXISTS pid INTEGER;
ALTER TABLE golden_repo_alias_locks ADD COLUMN IF NOT EXISTS ttl_seconds INTEGER;
