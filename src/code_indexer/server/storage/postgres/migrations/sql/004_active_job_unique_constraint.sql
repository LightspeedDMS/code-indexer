-- Migration 004: Add partial unique index for active job deduplication (Bug #536)
--
-- Prevents two nodes from simultaneously running the same (operation_type, repo_alias)
-- job. The partial index only covers pending/running jobs, so completed/failed jobs
-- are not affected and can have duplicates (expected — the same repo gets refreshed
-- many times over its lifetime).
--
-- This is a backward-compatible additive change (new index only).

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
    ON background_jobs (operation_type, repo_alias)
    WHERE status IN ('pending', 'running')
      AND repo_alias IS NOT NULL;
