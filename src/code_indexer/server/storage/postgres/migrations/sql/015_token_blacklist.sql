-- Bug #583: Token blacklist for cluster-wide JWT revocation
CREATE TABLE IF NOT EXISTS token_blacklist (
    jti TEXT PRIMARY KEY,
    blacklisted_at DOUBLE PRECISION NOT NULL
);
