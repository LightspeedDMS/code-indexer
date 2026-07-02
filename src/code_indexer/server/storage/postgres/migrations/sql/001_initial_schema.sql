-- Initial PostgreSQL schema for CIDX server.
-- Story #416: Database Migration System with Numbered SQL Files
--
-- Translated from SQLite schema in database_manager.py.
-- PostgreSQL-specific types:
--   TEXT timestamps -> TIMESTAMPTZ
--   TEXT JSON fields -> JSONB
--   INTEGER PRIMARY KEY (SQLite auto-incr) -> SERIAL PRIMARY KEY
--   BOOLEAN -> BOOLEAN (same, but no affinity quirks)

-- Migration tracking table (must come first)
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          SERIAL PRIMARY KEY,
    filename    TEXT        NOT NULL UNIQUE,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    TEXT        NOT NULL
);

-- Users and authentication

CREATE TABLE IF NOT EXISTS users (
    username        TEXT        PRIMARY KEY,
    password_hash   TEXT        NOT NULL,
    role            TEXT        NOT NULL,
    email           TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    oidc_identity   JSONB
);

CREATE TABLE IF NOT EXISTS user_api_keys (
    key_id      TEXT        PRIMARY KEY,
    username    TEXT        NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    key_hash    TEXT        NOT NULL,
    key_prefix  TEXT        NOT NULL,
    name        TEXT,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS user_mcp_credentials (
    credential_id       TEXT        PRIMARY KEY,
    username            TEXT        NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    client_id           TEXT        NOT NULL,
    client_secret_hash  TEXT        NOT NULL,
    client_id_prefix    TEXT        NOT NULL,
    name                TEXT,
    created_at          TIMESTAMPTZ NOT NULL,
    last_used_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_oidc_identities (
    username    TEXT        PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
    subject     TEXT        NOT NULL,
    email       TEXT,
    linked_at   TIMESTAMPTZ NOT NULL,
    last_login  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS invalidated_sessions (
    username    TEXT        NOT NULL,
    token_id    TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (username, token_id)
);

CREATE TABLE IF NOT EXISTS password_change_timestamps (
    username    TEXT        PRIMARY KEY,
    changed_at  TIMESTAMPTZ NOT NULL
);

-- Repository registry

CREATE TABLE IF NOT EXISTS global_repos (
    alias_name      TEXT        PRIMARY KEY,
    repo_name       TEXT        NOT NULL,
    repo_url        TEXT,
    index_path      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    last_refresh    TIMESTAMPTZ NOT NULL,
    enable_temporal BOOLEAN     DEFAULT FALSE,
    temporal_options JSONB,
    enable_scip     BOOLEAN     DEFAULT FALSE,
    next_refresh    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS golden_repos_metadata (
    alias                   TEXT        PRIMARY KEY NOT NULL,
    repo_url                TEXT        NOT NULL,
    default_branch          TEXT        NOT NULL,
    clone_path              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    enable_temporal         BOOLEAN     NOT NULL DEFAULT FALSE,
    temporal_options        JSONB,
    wiki_enabled            BOOLEAN     DEFAULT FALSE,
    category_id             INTEGER,
    category_auto_assigned  BOOLEAN     DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS golden_repo_indexes (
    id              SERIAL      PRIMARY KEY,
    repo_alias      TEXT        NOT NULL REFERENCES golden_repos_metadata(alias) ON DELETE CASCADE,
    index_path      TEXT        NOT NULL,
    branch          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    last_updated    TIMESTAMPTZ
);

-- Jobs

CREATE TABLE IF NOT EXISTS sync_jobs (
    job_id                  TEXT        PRIMARY KEY,
    username                TEXT        NOT NULL,
    user_alias              TEXT        NOT NULL,
    job_type                TEXT        NOT NULL,
    status                  TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    repository_url          TEXT,
    progress                INTEGER     DEFAULT 0,
    error_message           TEXT,
    phases                  JSONB,
    phase_weights           JSONB,
    current_phase           TEXT,
    progress_history        JSONB,
    recovery_checkpoint     JSONB,
    analytics_data          JSONB
);

CREATE TABLE IF NOT EXISTS background_jobs (
    job_id                      TEXT        PRIMARY KEY NOT NULL,
    operation_type              TEXT        NOT NULL,
    status                      TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL,
    started_at                  TIMESTAMPTZ,
    completed_at                TIMESTAMPTZ,
    result                      JSONB,
    error                       TEXT,
    progress                    INTEGER     NOT NULL DEFAULT 0,
    username                    TEXT        NOT NULL,
    is_admin                    BOOLEAN     NOT NULL DEFAULT FALSE,
    cancelled                   BOOLEAN     NOT NULL DEFAULT FALSE,
    repo_alias                  TEXT,
    resolution_attempts         INTEGER     NOT NULL DEFAULT 0,
    claude_actions              JSONB,
    failure_reason              TEXT,
    extended_error              JSONB,
    language_resolution_status  JSONB,
    progress_info               TEXT,
    metadata                    JSONB,
    executing_node              TEXT,
    claimed_at                  TIMESTAMPTZ,
    current_phase               TEXT,
    phase_detail                TEXT
);

-- SSH keys

CREATE TABLE IF NOT EXISTS ssh_keys (
    name            TEXT        PRIMARY KEY,
    fingerprint     TEXT        NOT NULL,
    key_type        TEXT        NOT NULL,
    private_path    TEXT        NOT NULL,
    public_path     TEXT        NOT NULL,
    public_key      TEXT,
    email           TEXT,
    description     TEXT,
    created_at      TIMESTAMPTZ,
    imported_at     TIMESTAMPTZ,
    is_imported     BOOLEAN     DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS ssh_key_hosts (
    key_name    TEXT    NOT NULL REFERENCES ssh_keys(name) ON DELETE CASCADE,
    hostname    TEXT    NOT NULL,
    PRIMARY KEY (key_name, hostname)
);

-- CI/CD integration

CREATE TABLE IF NOT EXISTS ci_tokens (
    platform        TEXT    PRIMARY KEY,
    encrypted_token TEXT    NOT NULL,
    base_url        TEXT
);

-- Self-monitoring

CREATE TABLE IF NOT EXISTS self_monitoring_scans (
    scan_id         TEXT        PRIMARY KEY NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ,
    status          TEXT        NOT NULL,
    log_id_start    INTEGER     NOT NULL,
    log_id_end      INTEGER,
    issues_created  INTEGER     NOT NULL DEFAULT 0,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS self_monitoring_issues (
    id                  SERIAL      PRIMARY KEY,
    scan_id             TEXT        NOT NULL REFERENCES self_monitoring_scans(scan_id) ON DELETE CASCADE,
    github_issue_number INTEGER,
    github_issue_url    TEXT,
    classification      TEXT        NOT NULL,
    title               TEXT        NOT NULL,
    error_codes         TEXT,
    fingerprint         TEXT        NOT NULL DEFAULT '',
    source_log_ids      TEXT        NOT NULL,
    source_files        TEXT,
    created_at          TIMESTAMPTZ NOT NULL
);

-- Research assistant

CREATE TABLE IF NOT EXISTS research_sessions (
    id                  TEXT        PRIMARY KEY,
    name                TEXT        NOT NULL,
    folder_path         TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    claude_session_id   TEXT
);

CREATE TABLE IF NOT EXISTS research_messages (
    id          SERIAL      PRIMARY KEY,
    session_id  TEXT        NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);

-- Diagnostics

CREATE TABLE IF NOT EXISTS diagnostic_results (
    category        TEXT    PRIMARY KEY,
    results_json    JSONB   NOT NULL,
    run_at          TIMESTAMPTZ NOT NULL
);

-- Repository categories

CREATE TABLE IF NOT EXISTS repo_categories (
    id          SERIAL      PRIMARY KEY,
    name        TEXT        UNIQUE NOT NULL,
    pattern     TEXT        NOT NULL,
    priority    INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ
);

-- Description refresh tracking

CREATE TABLE IF NOT EXISTS description_refresh_tracking (
    repo_alias                  TEXT        PRIMARY KEY,
    last_run                    TIMESTAMPTZ,
    next_run                    TIMESTAMPTZ,
    status                      TEXT        DEFAULT 'pending',
    error                       TEXT,
    last_known_commit           TEXT,
    last_known_files_processed  INTEGER,
    last_known_indexed_at       TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ
);

-- Dependency map tracking (singleton row, id=1)

CREATE TABLE IF NOT EXISTS dependency_map_tracking (
    id              INTEGER     PRIMARY KEY,
    last_run        TIMESTAMPTZ,
    next_run        TIMESTAMPTZ,
    status          TEXT        DEFAULT 'pending',
    commit_hashes   JSONB,
    error_message       TEXT,
    refinement_cursor   INTEGER     DEFAULT 0,
    refinement_next_run TIMESTAMPTZ
);

-- Wiki cache

CREATE TABLE IF NOT EXISTS wiki_cache (
    repo_alias      TEXT        NOT NULL,
    article_path    TEXT        NOT NULL,
    rendered_html   TEXT        NOT NULL,
    title           TEXT        NOT NULL,
    file_mtime      DOUBLE PRECISION NOT NULL,
    file_size       INTEGER     NOT NULL,
    rendered_at     TIMESTAMPTZ NOT NULL,
    metadata        JSONB,
    PRIMARY KEY (repo_alias, article_path)
);

CREATE TABLE IF NOT EXISTS wiki_sidebar_cache (
    repo_alias      TEXT        PRIMARY KEY,
    sidebar_json    JSONB       NOT NULL,
    max_mtime       DOUBLE PRECISION NOT NULL,
    built_at        TIMESTAMPTZ NOT NULL
);

-- Git credentials

CREATE TABLE IF NOT EXISTS user_git_credentials (
    credential_id   TEXT        PRIMARY KEY,
    username        TEXT        NOT NULL,
    forge_type      TEXT        NOT NULL,
    forge_host      TEXT        NOT NULL,
    encrypted_token TEXT        NOT NULL,
    git_user_name   TEXT,
    git_user_email  TEXT,
    forge_username  TEXT,
    name            TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    last_used_at    TIMESTAMPTZ,
    UNIQUE (username, forge_type, forge_host)
);

-- Refresh schedule (golden repo periodic refresh config)

CREATE TABLE IF NOT EXISTS refresh_schedule (
    id              SERIAL      PRIMARY KEY,
    repo_alias      TEXT        NOT NULL REFERENCES golden_repos_metadata(alias) ON DELETE CASCADE,
    interval_hours  INTEGER     NOT NULL DEFAULT 24,
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    last_run        TIMESTAMPTZ,
    next_run        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_alias)
);

-- Groups (user group management)

CREATE TABLE IF NOT EXISTS groups (
    id          SERIAL      PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id    INTEGER     NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    username    TEXT        NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    role        TEXT        NOT NULL DEFAULT 'member',
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, username)
);

CREATE TABLE IF NOT EXISTS group_repos (
    group_id    INTEGER     NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    repo_alias  TEXT        NOT NULL REFERENCES golden_repos_metadata(alias) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, repo_alias)
);

-- Audit logs

CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL      PRIMARY KEY,
    username    TEXT,
    action      TEXT        NOT NULL,
    resource    TEXT,
    details     JSONB,
    ip_address  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Cluster nodes (Epic #408: CIDX clusterization)

CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id         TEXT        PRIMARY KEY,
    hostname        TEXT        NOT NULL,
    port            INTEGER     NOT NULL DEFAULT 8000,
    status          TEXT        NOT NULL DEFAULT 'active',
    role            TEXT        NOT NULL DEFAULT 'worker',
    last_heartbeat  TIMESTAMPTZ,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB
);

-- Upgrade registry (tracks software upgrade history)

CREATE TABLE IF NOT EXISTS upgrade_registry (
    id              SERIAL      PRIMARY KEY,
    node_id         TEXT        NOT NULL,
    version_from    TEXT        NOT NULL,
    version_to      TEXT        NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'upgrading',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    error_message   TEXT
);

-- Indexes for performance

CREATE INDEX IF NOT EXISTS idx_background_jobs_status
    ON background_jobs(status);

CREATE INDEX IF NOT EXISTS idx_background_jobs_status_created
    ON background_jobs(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_background_jobs_completed_status
    ON background_jobs(completed_at, status);

CREATE INDEX IF NOT EXISTS idx_background_jobs_op_repo_status
    ON background_jobs(operation_type, repo_alias, status);

CREATE INDEX IF NOT EXISTS idx_background_jobs_user_created
    ON background_jobs(username, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_background_jobs_created
    ON background_jobs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_api_keys_username
    ON user_api_keys(username);

CREATE INDEX IF NOT EXISTS idx_user_mcp_credentials_username
    ON user_mcp_credentials(username);

CREATE INDEX IF NOT EXISTS idx_user_mcp_credentials_client_id
    ON user_mcp_credentials(client_id);

CREATE INDEX IF NOT EXISTS idx_research_messages_session_id
    ON research_messages(session_id);

CREATE INDEX IF NOT EXISTS idx_self_monitoring_scans_started_at
    ON self_monitoring_scans(started_at);

CREATE INDEX IF NOT EXISTS idx_self_monitoring_issues_scan_id
    ON self_monitoring_issues(scan_id);

CREATE INDEX IF NOT EXISTS idx_audit_logs_username
    ON audit_logs(username);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
    ON audit_logs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cluster_nodes_status
    ON cluster_nodes(status);
