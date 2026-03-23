-- Groups access schema for CIDX server (Story #415).
--
-- Replaces the generic groups/group_members/group_repos tables from migration 001
-- with the full GroupAccessManager schema:
--   groups                 - named groups with is_default flag
--   user_group_membership  - 1:1 user-to-group assignments
--   repo_group_access      - many-to-many repo-group access grants
--   audit_logs             - administrative audit trail
--
-- The original audit_logs table from migration 001 used a different schema
-- (username/action/resource columns). This migration drops it and recreates it
-- with the GroupAccessManager schema (admin_id/action_type/target_type/target_id).

-- Drop old tables from migration 001 that are being replaced.
-- CASCADE drops dependent indexes automatically.
DROP TABLE IF EXISTS group_repos CASCADE;
DROP TABLE IF EXISTS group_members CASCADE;
DROP TABLE IF EXISTS audit_logs CASCADE;
-- Keep old groups table dropped too since we recreate with different schema.
DROP TABLE IF EXISTS groups CASCADE;

-- Groups table: named groups with is_default flag.
-- is_default=TRUE means the group was created at bootstrap and cannot be deleted.
CREATE TABLE IF NOT EXISTS groups (
    id          SERIAL      PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    description TEXT        NOT NULL DEFAULT '',
    is_default  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_groups_name ON groups(LOWER(name));

-- User-to-group membership: one user belongs to exactly one group.
-- user_id is TEXT (matches username in users table) and is PRIMARY KEY
-- to enforce the 1:1 constraint.
CREATE TABLE IF NOT EXISTS user_group_membership (
    user_id     TEXT        PRIMARY KEY,
    group_id    INTEGER     NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assigned_by TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_group_membership_group_id
    ON user_group_membership(group_id);

-- Repository-to-group access: many-to-many.
-- cidx-meta access is implicit (not stored) and always allowed.
CREATE TABLE IF NOT EXISTS repo_group_access (
    repo_name   TEXT        NOT NULL,
    group_id    INTEGER     NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by  TEXT,
    PRIMARY KEY (repo_name, group_id)
);

CREATE INDEX IF NOT EXISTS idx_repo_group_access_group_id
    ON repo_group_access(group_id);

CREATE INDEX IF NOT EXISTS idx_repo_group_access_repo_name
    ON repo_group_access(repo_name);

-- Audit logs: administrative action trail.
-- Unified table used by both GroupAccessManager and AuditLogService.
CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL      PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    admin_id    TEXT        NOT NULL,
    action_type TEXT        NOT NULL,
    target_type TEXT        NOT NULL,
    target_id   TEXT        NOT NULL,
    details     TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp
    ON audit_logs(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_audit_logs_action_type
    ON audit_logs(action_type);
