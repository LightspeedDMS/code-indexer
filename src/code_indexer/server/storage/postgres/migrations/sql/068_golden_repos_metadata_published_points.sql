-- Migration 068: reference size for the refresh volume alarm (Story #2118).
--
-- published_points is the number of points in a golden repo's last verified
-- published snapshot, written by the refresh scheduler after each verified
-- publish. The volume alarm compares a repo's 24 hour refresh embedding
-- volume against a fraction of it. NULL (a repo not refreshed since this
-- column exists, or a row written by a node that predates it) means the
-- alarm's fixed floor applies.
--
-- Nullable add-column only: backward compatible with rolling restarts.

ALTER TABLE golden_repos_metadata ADD COLUMN IF NOT EXISTS published_points BIGINT;
