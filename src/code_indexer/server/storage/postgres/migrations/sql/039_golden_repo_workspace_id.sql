-- Migration 039: the content service workspace_id retention for golden repos
-- (issue-65021).
--
-- SCIP compile builds are delegated to the sandbox service, which operates on a
-- the content service workspace addressed as `sb-{workspace_id}`. cidx therefore has
-- to hand the sandbox service the SAME workspace_id that the content service used to provision
-- the golden repo -- but golden repo metadata previously kept only the on-disk
-- clone_path, not the the content service handle.
--
-- Additive, backward-compatible change: a nullable `workspace_id` column.
-- Existing rows stay NULL until the id is captured at (re-)registration or
-- backfilled; the delegated SCIP backend fails-soft (skips SCIP) for a repo
-- whose workspace_id is not yet retained. The SQLite counterpart lives in
-- database_manager.py (_migrate_golden_repos_metadata_workspace_id) and
-- sqlite_backends.py (GoldenRepoMetadataSqliteBackend.ensure_table_exists).

ALTER TABLE golden_repos_metadata
    ADD COLUMN IF NOT EXISTS workspace_id TEXT;
