-- Migration 011: Activated repo metadata for cluster mode.
-- Bug #587: Store activated repo metadata in PostgreSQL so all cluster nodes
-- can see repos activated on any node.
-- Backward compatible: new table only, no modifications to existing schema.

CREATE TABLE IF NOT EXISTS activated_repos (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL,
    user_alias TEXT NOT NULL,
    golden_repo_alias TEXT,
    repo_path TEXT NOT NULL,
    current_branch TEXT DEFAULT 'main',
    activated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    git_committer_email TEXT,
    ssh_key_used BOOLEAN DEFAULT FALSE,
    is_composite BOOLEAN DEFAULT FALSE,
    wiki_enabled BOOLEAN DEFAULT FALSE,
    metadata_json JSONB,
    UNIQUE(username, user_alias)
);

CREATE INDEX IF NOT EXISTS idx_activated_repos_username ON activated_repos(username);
CREATE INDEX IF NOT EXISTS idx_activated_repos_golden ON activated_repos(golden_repo_alias);
