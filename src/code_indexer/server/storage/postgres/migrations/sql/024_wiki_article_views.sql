-- Migration 024: wiki_article_views table for cluster mode.
-- The SQLite backend and PG backend both create this table inline,
-- but a proper migration ensures it exists before data migration runs.
-- Backward compatible: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS wiki_article_views (
    repo_alias TEXT NOT NULL,
    article_path TEXT NOT NULL,
    real_views INTEGER DEFAULT 0,
    first_viewed_at TIMESTAMP WITH TIME ZONE,
    last_viewed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (repo_alias, article_path)
);
