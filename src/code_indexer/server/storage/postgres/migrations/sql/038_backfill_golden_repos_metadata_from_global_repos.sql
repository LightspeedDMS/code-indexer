-- Migration 038: Backfill golden_repos_metadata from global_repos
--
-- The PostgreSQL migration (001_initial_schema.sql) created the
-- golden_repos_metadata table from scratch without importing existing
-- repos from global_repos. The golden-repo seeder reads from
-- golden_repos_metadata via the API, so every repo that existed ONLY in
-- global_repos appeared "new" on each seeder run, triggering a redundant
-- clone + lifecycle Claude call (~$3/job on Opus, ~800 jobs/day).
--
-- global_repos stores aliases with a "-global" suffix
-- (e.g. "account-customizations-global"); golden_repos_metadata stores
-- bare aliases ("account-customizations"). This migration copies missing
-- repos from global_repos into golden_repos_metadata, stripping the
-- suffix and mapping columns.
--
-- default_branch defaults to 'master' for backfilled rows (the seeder
-- corrects this on the next refresh cycle). clone_path is derived from
-- index_path (already the on-disk location). Idempotent: skips any alias
-- already present in golden_repos_metadata.

INSERT INTO golden_repos_metadata (
    alias, repo_url, default_branch, clone_path,
    created_at, enable_temporal, temporal_options
)
SELECT
    regexp_replace(g.alias_name, '-global$', ''),
    g.repo_url,
    'master',
    g.index_path,
    g.created_at,
    COALESCE(g.enable_temporal, false),
    g.temporal_options
FROM global_repos g
WHERE regexp_replace(g.alias_name, '-global$', '') NOT IN (
    SELECT alias FROM golden_repos_metadata
);
