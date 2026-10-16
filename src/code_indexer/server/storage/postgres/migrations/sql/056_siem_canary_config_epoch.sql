-- SIEM delivery (Bug #2018): a canary confirmation is valid only for the
-- configuration lifetime that produced it.  The canary records the committed
-- siem_delivery.arming_epoch it was run under; arming requires it to equal
-- the committed epoch (services/siem_delivery/state_store.py).
--
-- '' is the lifetime of a configuration saved before the epoch existed, so
-- an already armed destination stays armed across the upgrade.
--
-- The SQLite schema (services/siem_delivery/db.py) adds the same column.
--
-- Backward-compatible additive change only: ADD COLUMN IF NOT EXISTS with a
-- default.

ALTER TABLE siem_delivery_state
    ADD COLUMN IF NOT EXISTS canary_config_epoch TEXT NOT NULL DEFAULT '';
