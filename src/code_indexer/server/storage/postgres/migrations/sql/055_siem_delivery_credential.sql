-- SIEM delivery: the SecOps service-account key, configured in the Web UI and
-- stored ENCRYPTED (services/token_encryption.py, the server's stored-secret
-- mechanism).  One row (id = 1).  Only the non-secret identity columns are
-- ever read back for display.
--
-- The SQLite schema (services/siem_delivery/db.py, created in groups.db)
-- uses the same table and column names.
--
-- Backward-compatible additive change only: CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS siem_delivery_credential (
    id INTEGER PRIMARY KEY,
    credential_id TEXT NOT NULL,
    encrypted_key TEXT NOT NULL,
    -- HMAC-SHA256(encryption key, "siem-credential"): tells a key mismatch
    -- (another node or a rotated cluster secret) from a corrupt row
    key_check TEXT NOT NULL,
    client_email TEXT NOT NULL,
    private_key_id TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL
);
