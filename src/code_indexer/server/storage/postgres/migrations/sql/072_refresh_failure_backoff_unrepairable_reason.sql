-- Migration 072: why a refresh failure cannot self-heal (Story #2126).
--
-- Set with a recorded refresh failure that no automatic repair can fix --
-- e.g. an established collection's chunk store is missing and no usable
-- published snapshot exists to restore it from -- so the health surface can
-- name the repository and the reason. Every recorded failure replaces it
-- (an ordinary failure clears it); a verified publish deletes the row or
-- clears it on re-arm.
--
-- Nullable add-column only: backward compatible with rolling restarts (a
-- node that predates it never reads or writes the column).

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS unrepairable_reason TEXT;
