-- Story #728: Add lifecycle_schema_version column to description_refresh_tracking.
-- Tracks which lifecycle metadata schema version each repo's .md file was generated with.
-- Used by the backfill scheduler to detect repos that need re-generation.
-- Idempotent: ADD COLUMN IF NOT EXISTS is a no-op when the column already exists.
ALTER TABLE description_refresh_tracking
    ADD COLUMN IF NOT EXISTS lifecycle_schema_version INTEGER DEFAULT 0;
