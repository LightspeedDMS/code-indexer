-- Migration 071: (username, timestamp) index for user-filtered Query
-- Analytics exports (Bug #2106).
--
-- An export reads search_event_log in batches keyed on (timestamp, id). With a
-- user filter, idx_sel_user (username only) cannot bound a batch by its
-- keyset, so every batch re-reads and re-sorts all of that user's rows. Led by
-- username with timestamp next, each batch is one range search.
--
-- Create-index only: backward compatible with rolling restarts.

CREATE INDEX IF NOT EXISTS idx_sel_user_ts
    ON search_event_log (username, timestamp);
