-- Record the instant each account was created.  Credentials issued before it
-- belong to an earlier account with the same name and are refused.
-- Backward compatible: nullable, ADD COLUMN IF NOT EXISTS; existing accounts
-- keep NULL (no restriction).

ALTER TABLE users ADD COLUMN IF NOT EXISTS account_created_at TEXT;
