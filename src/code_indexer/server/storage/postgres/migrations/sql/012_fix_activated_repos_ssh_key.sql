-- Migration 012: Fix ssh_key_used column type in activated_repos.
-- Bug #587: ssh_key_used stores SSH key name (string), not boolean.
-- Backward compatible: ALTER COLUMN with USING cast, no data loss.

ALTER TABLE activated_repos
    ALTER COLUMN ssh_key_used DROP DEFAULT,
    ALTER COLUMN ssh_key_used TYPE TEXT USING CASE WHEN ssh_key_used THEN 'true' ELSE NULL END,
    ALTER COLUMN ssh_key_used SET DEFAULT NULL;
