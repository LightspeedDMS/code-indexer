-- Migration 026: Add actor_username column to background_jobs (AC12, Story #1032)
--
-- Tracks WHO actually triggered a background job (the actor) separately from
-- WHOSE resource is being operated on (username/submitter_username).
-- Required for: admin deactivating other users' repos, reaper auto-deactivation,
-- and any future operation where the initiator differs from the resource owner.
--
-- Backward-compatible: existing rows get NULL (no actor known = treated as same as owner).
-- NOT NULL constraint intentionally omitted to allow rolling restarts on shared schema.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'background_jobs'
          AND column_name = 'actor_username'
    ) THEN
        ALTER TABLE background_jobs ADD COLUMN actor_username TEXT NULL;
    END IF;
END
$$;
