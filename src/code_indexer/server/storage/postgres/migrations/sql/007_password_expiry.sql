-- Story #565: Password expiry - add password_changed_at column
-- Backward compatible: ALTER TABLE ADD COLUMN IF NOT EXISTS

ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TEXT;
