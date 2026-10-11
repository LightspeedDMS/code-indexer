-- Migration 070: escalation counter of an OWED refresh retry (Bug #2125).
--
-- An owed retry is a pending refresh trigger for files a published refresh
-- still owes (its indexing child recorded failed files), or for a
-- trigger-only repository whose refresh failed on an ordinary error. It is
-- NOT a failure: consecutive_failure_count, which defers every system
-- submission of the alias, stays untouched. owed_retry_count escalates the
-- retry interval instead (same bounded formula), and a clean verified
-- publish resets it (the row is deleted, or a surviving row is re-armed
-- with the count back at 0).
--
-- Add-column with a default only: backward compatible with rolling
-- restarts (a node that predates it never reads or writes the column).

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS owed_retry_count INTEGER NOT NULL DEFAULT 0;
