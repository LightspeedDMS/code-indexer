-- Migration 038: Add embedding_call_stats table (Story #1418)
--
-- Vendor cost reconciliation: see docs/architecture-invariants.md's
-- "Embedding & Reranker Call Tracking" section for the full architecture.
--
-- Records every REAL (non-cached, non-suppressed) embedding/reranker call
-- to VoyageAI/Cohere for vendor cost reconciliation: provider, model,
-- token/item counts, batch size, triggering purpose, golden-repo/job
-- context, and cluster node id. Cache hits and coalesced-away duplicate
-- requests must NEVER be recorded -- only real vendor-billed HTTP calls.
--
-- Additive only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
-- No DROP/RENAME/TYPE-CHANGE (backward-compatible per CLAUDE.md rolling-
-- upgrade safety rules).
--
-- Indexes support the query() filter signature (provider, purpose,
-- golden_repo_alias, job_id, time range) and the delete_where() retention
-- sweep (occurred_at).

CREATE TABLE IF NOT EXISTS embedding_call_stats (
    id                 BIGSERIAL PRIMARY KEY,
    provider           TEXT NOT NULL,
    call_type          TEXT NOT NULL,
    model              TEXT NOT NULL,
    item_count         INTEGER NOT NULL,
    token_count        INTEGER NOT NULL,
    batch_size         INTEGER NOT NULL,
    purpose            TEXT NOT NULL,
    golden_repo_alias  TEXT,
    job_id             TEXT,
    node_id            TEXT,
    success            BOOLEAN NOT NULL,
    latency_ms         INTEGER NOT NULL,
    occurred_at        DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ecs_occurred_at
    ON embedding_call_stats (occurred_at);

CREATE INDEX IF NOT EXISTS idx_ecs_provider
    ON embedding_call_stats (provider);

CREATE INDEX IF NOT EXISTS idx_ecs_golden_repo_alias
    ON embedding_call_stats (golden_repo_alias);

CREATE INDEX IF NOT EXISTS idx_ecs_job_id
    ON embedding_call_stats (job_id);

CREATE INDEX IF NOT EXISTS idx_ecs_purpose
    ON embedding_call_stats (purpose);
