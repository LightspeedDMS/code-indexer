-- Migration 069: purpose/time-led index for the fleet refresh volume read
-- (Story #2118).
--
-- The /health volume alarm sums refresh-purpose item_count per
-- golden_repo_alias over the last 24 hours for the whole fleet, filtering on
-- purpose and occurred_at only. Migration 067's index leads with
-- golden_repo_alias (it serves the per-alias pause gate) and cannot bound
-- that read. Led by (purpose, occurred_at), this one is a range search; the
-- trailing (golden_repo_alias, item_count) make it covering (index-only).
--
-- Create-index only: backward compatible with rolling restarts.

CREATE INDEX IF NOT EXISTS idx_ecs_purpose_time_alias
    ON embedding_call_stats (purpose, occurred_at, golden_repo_alias, item_count);
