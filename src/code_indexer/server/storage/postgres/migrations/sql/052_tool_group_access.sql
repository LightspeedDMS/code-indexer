-- Migration 052: Per-tool, per-group MCP access control (Story #1593, AC1).
--
-- Introduces tool_group_access, mirroring repo_group_access's shape:
-- group_id/tool_name identify the grant, `allowed` is an explicit NOT
-- NULL boolean (Decision 9 -- row presence alone cannot distinguish
-- "never seeded" from "explicitly revoked", which would let a
-- fail-closed idempotent seeder silently un-revoke an admin's explicit
-- decision on every restart), granted_by/granted_at record who/when.
--
-- group_id references groups(id) ON DELETE CASCADE, so a deleted group's
-- grants are removed automatically in PostgreSQL. SQLite has no cascade
-- wired for this table -- GroupAccessManager.delete_group() explicitly
-- deletes matching tool_group_access rows there instead.

CREATE TABLE IF NOT EXISTS tool_group_access (
    group_id    INTEGER     NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    tool_name   TEXT        NOT NULL,
    allowed     BOOLEAN     NOT NULL,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by  TEXT,
    PRIMARY KEY (group_id, tool_name)
);

CREATE INDEX IF NOT EXISTS idx_tool_group_access_group_id
    ON tool_group_access(group_id);

CREATE INDEX IF NOT EXISTS idx_tool_group_access_tool_name
    ON tool_group_access(tool_name);
