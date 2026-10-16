-- SIEM delivery (Bug #2018): a strictly increasing canary run ordinal.
-- Each canary run takes the next ordinal under the state-row lock BEFORE its
-- send (canary_issued_seq); the recorded run keeps it (canary_run_seq).  A
-- run that completes after a later-issued run was recorded is refused, so an
-- older run can never replace a newer one -- whatever the clock precision.
--
-- The SQLite schema (services/siem_delivery/db.py) adds the same columns.
--
-- Backward-compatible additive change only: ADD COLUMN IF NOT EXISTS with a
-- default.

ALTER TABLE siem_delivery_state
    ADD COLUMN IF NOT EXISTS canary_issued_seq BIGINT NOT NULL DEFAULT 0;
ALTER TABLE siem_delivery_state
    ADD COLUMN IF NOT EXISTS canary_run_seq BIGINT NOT NULL DEFAULT 0;
