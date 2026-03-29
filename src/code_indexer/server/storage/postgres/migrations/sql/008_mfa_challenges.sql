-- C3: MFA challenge tokens for cluster mode
-- Backward compatible: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS

CREATE TABLE IF NOT EXISTS mfa_challenges (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    client_ip TEXT NOT NULL,
    redirect_url TEXT DEFAULT '/admin/',
    created_at DOUBLE PRECISION NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    oauth_client_id TEXT,
    oauth_redirect_uri TEXT,
    oauth_code_challenge TEXT,
    oauth_state TEXT
);

CREATE INDEX IF NOT EXISTS idx_mfa_challenges_created ON mfa_challenges(created_at);
