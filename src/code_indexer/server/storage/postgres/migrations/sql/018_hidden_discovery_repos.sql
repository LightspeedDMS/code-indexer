-- Story #719: Hide Repositories from Auto-Discovery View
CREATE TABLE IF NOT EXISTS hidden_discovery_repos (
    id SERIAL PRIMARY KEY,
    repo_identifier TEXT NOT NULL UNIQUE,
    hidden_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hidden_discovery_repos_identifier
    ON hidden_discovery_repos(repo_identifier);
