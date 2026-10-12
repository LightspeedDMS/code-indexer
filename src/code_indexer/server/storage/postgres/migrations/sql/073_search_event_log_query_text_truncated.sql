-- Migration 073: mark search events whose query text was truncated (Bug #2114).
--
-- query_text is stored capped at 500 characters. TRUE when the search's text
-- was longer and was cut, FALSE when it was stored whole. Rows written before
-- this column existed read NULL (unknown). The Query Analytics Export carries
-- the column next to query_text.
--
-- Nullable add-column only: backward compatible with rolling restarts (a
-- node that predates it never reads or writes the column).

ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS query_text_truncated BOOLEAN;
