-- Migration 027: Add private_key column to ssh_keys (Bug #1072, Chunk 1)
--
-- Stores the encrypted private key content directly in the database so that
-- cluster nodes without access to the original filesystem path can still
-- use SSH keys.
--
-- Backward-compatible: existing rows get NULL (no stored content = file-path-only mode).
-- NOT NULL constraint intentionally omitted to allow rolling restarts on shared schema.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ssh_keys' AND column_name = 'private_key'
    ) THEN
        ALTER TABLE ssh_keys ADD COLUMN private_key TEXT NULL;
    END IF;
END
$$;
