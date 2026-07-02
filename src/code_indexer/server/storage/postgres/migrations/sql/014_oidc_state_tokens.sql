-- Bug #576: OIDC state tokens for cluster mode
CREATE TABLE IF NOT EXISTS oidc_state_tokens (
    state_token TEXT PRIMARY KEY,
    state_data TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
