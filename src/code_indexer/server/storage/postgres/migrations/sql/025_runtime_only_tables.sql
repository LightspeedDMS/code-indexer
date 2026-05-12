-- Migration 025: Create runtime-only tables needed by SQLite-to-PostgreSQL
-- data migration tool (cluster-migrate.sh).
--
-- These tables are normally created at server startup by their respective
-- Python backends (_ensure_schema()). However, during fresh cluster migration,
-- schema migrations run BEFORE the server starts, so these tables must exist
-- before the data migration tool can INSERT rows.
--
-- All statements are idempotent (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).

-- ---------------------------------------------------------------------------
-- OAuth tables (normally created by OAuthBackend._ensure_schema)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (client_id) REFERENCES oauth_clients (client_id)
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    access_token TEXT UNIQUE NOT NULL,
    refresh_token TEXT UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    hard_expires_at TEXT NOT NULL,
    FOREIGN KEY (client_id) REFERENCES oauth_clients (client_id)
);

CREATE INDEX IF NOT EXISTS idx_tokens_access ON oauth_tokens (access_token);

CREATE TABLE IF NOT EXISTS oidc_identity_links (
    username TEXT NOT NULL PRIMARY KEY,
    subject TEXT NOT NULL UNIQUE,
    email TEXT,
    linked_at TEXT NOT NULL,
    last_login TEXT
);

CREATE INDEX IF NOT EXISTS idx_oidc_subject ON oidc_identity_links (subject);

-- ---------------------------------------------------------------------------
-- Refresh token tables (normally created by RefreshTokenBackend._ensure_schema)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS token_families (
    family_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    is_revoked BOOLEAN DEFAULT FALSE,
    revocation_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_family_username ON token_families (username);
CREATE INDEX IF NOT EXISTS idx_family_revoked ON token_families (is_revoked);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    username TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_used BOOLEAN DEFAULT FALSE,
    used_at TEXT,
    parent_token_id TEXT,
    FOREIGN KEY (family_id) REFERENCES token_families (family_id)
);

CREATE INDEX IF NOT EXISTS idx_token_family ON refresh_tokens (family_id);
CREATE INDEX IF NOT EXISTS idx_token_username ON refresh_tokens (username);
CREATE INDEX IF NOT EXISTS idx_token_hash ON refresh_tokens (token_hash);

-- ---------------------------------------------------------------------------
-- SCIP audit table (normally created by ScipAuditBackend._ensure_schema)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scip_dependency_installations (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    job_id VARCHAR(36) NOT NULL,
    repo_alias VARCHAR(255) NOT NULL,
    project_path VARCHAR(255),
    project_language VARCHAR(50),
    project_build_system VARCHAR(50),
    package VARCHAR(255) NOT NULL,
    command TEXT NOT NULL,
    reasoning TEXT,
    username VARCHAR(255),
    node_id VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_timestamp ON scip_dependency_installations (timestamp);
CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_repo_alias ON scip_dependency_installations (repo_alias);
CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_job_id ON scip_dependency_installations (job_id);
CREATE INDEX IF NOT EXISTS idx_scip_audit_pg_project_language ON scip_dependency_installations (project_language);
