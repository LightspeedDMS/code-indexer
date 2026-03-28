-- Story #558: MFA tables for TOTP authentication
-- Backward compatible: CREATE TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN

CREATE TABLE IF NOT EXISTS user_mfa (
    user_id TEXT UNIQUE NOT NULL,
    encrypted_secret TEXT NOT NULL,
    key_id INTEGER DEFAULT 1,
    mfa_enabled BOOLEAN DEFAULT FALSE,
    last_used_counter INTEGER,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_mfa_user_id ON user_mfa(user_id);

CREATE TABLE IF NOT EXISTS user_recovery_codes (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    used_at TIMESTAMP,
    used_ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_recovery_codes_user ON user_recovery_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_recovery_codes_unused
    ON user_recovery_codes(user_id) WHERE used_at IS NULL;
