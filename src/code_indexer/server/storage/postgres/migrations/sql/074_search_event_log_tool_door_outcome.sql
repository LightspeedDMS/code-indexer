-- Migration 074: tool, door and outcome dimensions of search events (Story #2117).
--
-- tool        the MCP tool or REST/Web endpoint that ran the search
--             (e.g. search_code, POST /api/query, web:query)
-- door        the entry point: mcp, rest or web
-- outcome     success or failure
-- error_class a short neutral token for a failure (e.g. timeout,
--             repository_failed); never a message, path, query or user data
--
-- Rows written before these columns existed read NULL (unknown). Nullable
-- add-column only: backward compatible with rolling restarts (a node that
-- predates them never reads or writes them).

ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS tool TEXT;
ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS door TEXT;
ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE search_event_log
    ADD COLUMN IF NOT EXISTS error_class TEXT;
