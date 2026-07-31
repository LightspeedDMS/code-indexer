-- Migration 039: workspace-mcp workspace_id retention for golden repos
-- (EVO-65021).
--
-- SCIP compile builds are delegated to sandbox-mcp, which operates on a
-- workspace-mcp workspace addressed as `sb-{workspace_id}`. cidx therefore has
-- to hand sandbox-mcp the SAME workspace_id that workspace-mcp used to provision
-- the golden repo -- but golden repo metadata previously kept only the on-disk
-- clone_path, not the workspace-mcp handle.
--
-- Additive, backward-compatible change: a nullable `workspace_id` column.
-- Existing rows stay NULL until the id is captured at (re-)registration or
-- backfilled; the delegated SCIP backend fails-soft (skips SCIP) for a repo
-- whose workspace_id is not yet retained. The SQLite counterpart lives in
-- database_manager.py (_migrate_golden_repos_metadata_workspace_id) and
-- sqlite_backends.py (GoldenRepoMetadataSqliteBackend.ensure_table_exists).

ALTER TABLE golden_repos_metadata
    ADD COLUMN IF NOT EXISTS workspace_id TEXT;
