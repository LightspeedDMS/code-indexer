-- Story #923 AC9: Add last_used_otp_counter to user_mfa for elevation
-- replay prevention.
--
-- The existing last_used_counter column tracks replay within the standard
-- TOTP verification window.  The elevation endpoint (POST /auth/elevate)
-- requires a separate CAS guard so that any given OTP can be used at most
-- once across the entire elevation flow, even when called concurrently from
-- different nodes in a cluster.
--
-- last_used_otp_counter stores int(unix_time // 30) — the TOTP time-step
-- index.  Two concurrent elevation requests with the same code produce the
-- same counter value; only the first UPDATE that wins the CAS race returns
-- a row, so the second is rejected.
--
-- All changes are idempotent (ALTER TABLE ADD COLUMN IF NOT EXISTS) so
-- this migration is safe to run against databases that already have the
-- column.

ALTER TABLE user_mfa
    ADD COLUMN IF NOT EXISTS last_used_otp_counter INTEGER;
