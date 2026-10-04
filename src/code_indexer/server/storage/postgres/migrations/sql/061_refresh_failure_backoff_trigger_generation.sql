-- Migration 061: store-ordered generation for deferred refresh triggers
-- (Bug #2022 Gap 4).
--
-- Every deferral of a system refresh trigger sets trigger_generation to a
-- fresh value from refresh_trigger_generation_seq, in the same UPDATE that
-- marks the trigger pending. A refresh cycle captures the generation from
-- the store before it reads its source, and a verified publish deletes the
-- row only while the generation is still the captured one; a trigger
-- deferred during the cycle changed it, so the row survives re-armed. The
-- sequence is store-wide, so a value is never reused -- not even by a row
-- that was deleted and recreated while a cycle was in flight. This
-- replaces a comparison of wall clocks taken on different nodes, which
-- clock skew could invert. pending_marked_at (migration 060) is
-- informational only.
--
-- Resolution assumes every writer of refresh_failure_backoff_state advances
-- the generation when it marks a trigger. That holds because no released
-- version writes this table: migrations 058-061 and all of their code ship
-- together in 12.82.0 (this migration was amended in place before any
-- deployment).
--
-- Add-column and create-sequence only: backward compatible with rolling
-- restarts.

ALTER TABLE refresh_failure_backoff_state
    ADD COLUMN IF NOT EXISTS trigger_generation BIGINT NOT NULL DEFAULT 0;

CREATE SEQUENCE IF NOT EXISTS refresh_trigger_generation_seq;
