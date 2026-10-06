-- Migration 066: how an MFA challenge's first factor was proven.
--
-- 'password' (a password login door) or 'sso' (the OIDC callback). Codes at
-- an SSO-started challenge are throttled under their own key, never the
-- password-login key, and the login's audit row names the real method.
-- Nullable and additive: NULL (a row written by a node that predates this
-- column) is read as 'password'. Backward compatible with rolling restarts.

ALTER TABLE mfa_challenges ADD COLUMN IF NOT EXISTS first_factor TEXT;
