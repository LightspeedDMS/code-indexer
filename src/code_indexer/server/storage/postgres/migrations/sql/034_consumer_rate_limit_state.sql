-- Migration 034: Consumer rate limit state table (PR #1332 review fix)
--
-- Dedicated table for the admission-control PerConsumerRateLimiter, storing
-- per-consumer token bucket state keyed by a SHA-256 hash of the caller's
-- credential. Deliberately NOT the auth login-limiter's token_bucket_state
-- table (whose PK column "username" holds real usernames) -- co-mingling
-- hashed, non-identity keys with real usernames in one table would be a
-- cross-domain collision landmine. The key column is named "consumer_key"
-- (not "username") so its non-identity nature is unambiguous.
--
-- Column shape mirrors token_bucket_state exactly; only the table/key-column
-- names differ.

CREATE TABLE IF NOT EXISTS consumer_rate_limit_state (
    consumer_key TEXT             PRIMARY KEY,
    tokens       DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    last_refill  DOUBLE PRECISION NOT NULL,
    last_access  DOUBLE PRECISION NOT NULL
);
