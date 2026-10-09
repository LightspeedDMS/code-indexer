-- Migration 067: index for the per-repository refresh volume alarm (Story #2118).
--
-- The volume alarm sums item_count of refresh-purpose embedding calls per
-- golden_repo_alias over the last 24 hours, and the pause gate does the same
-- for one alias before every system-triggered refresh. This composite index
-- serves both reads as a single indexed query instead of a table scan.
--
-- Create-index only: backward compatible with rolling restarts.

CREATE INDEX IF NOT EXISTS idx_ecs_alias_purpose_time
    ON embedding_call_stats (golden_repo_alias, purpose, occurred_at);
