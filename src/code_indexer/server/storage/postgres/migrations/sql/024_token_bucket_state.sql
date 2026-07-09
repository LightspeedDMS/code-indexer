-- Migration 024: Token bucket state table for cross-node rate limiting (Task #26)
--
-- Stores per-username token bucket state so all cluster nodes share
-- rate-limit enforcement. Nodes read/update via atomic UPDATE with RETURNING.
-- Rows are created on first access with full capacity.

CREATE TABLE IF NOT EXISTS token_bucket_state (
    username    TEXT             PRIMARY KEY,
    tokens      DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    last_refill DOUBLE PRECISION NOT NULL,
    last_access DOUBLE PRECISION NOT NULL
);
