-- Migration 032: Add search_embed_event table (Story #1293, Epic #1288)
--
-- Additive only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
-- No DROP/RENAME/TYPE-CHANGE (backward-compatible per CLAUDE.md rolling-upgrade
-- safety rules).
--
-- Durable, phantom-free source of truth for every query-embedding decision on
-- every server query path (replaces the per-node in-memory
-- QueryEmbeddingCacheMetrics tallies that overcount under coalescing/fan-out).
-- One row per NEEDED embed (never a phantom hit).
--
-- correlation_id is NEVER null (application layer guarantees a UUID fallback
-- when no request-scoped correlation id is available -- see
-- search_embed_event_emit.py). role/outcome encode the Story #1293
-- (path x outcome) -> (role, live_batch_id) decision table:
--   outcome: hit | miss | shadow_hit | shadow_miss | bypass | error
--   role:    owner | joiner | warm_hit | direct
-- live_batch_id is non-NULL only for members of one coalesced provider HTTP
-- batch (the owner's miss and its joiners); NULL for warm_hit / direct / bypass
-- / error rows.
--
-- Indexes kept MINIMAL for this story (Story #1293 B4 benchmarks/prunes
-- further in a later pass): timestamp for retention pruning and windowed
-- queries, correlation_id for the search_event_log join-integrity check
-- (AC-B1/B2).

CREATE TABLE IF NOT EXISTS search_embed_event (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       DOUBLE PRECISION NOT NULL,
    correlation_id  TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT,
    config_digest   TEXT,
    cache_mode      TEXT,
    outcome         TEXT NOT NULL,
    role            TEXT NOT NULL,
    live_batch_id   TEXT,
    embed_key       TEXT,
    long_key        BOOLEAN,
    latency_ms      INTEGER,
    shadow_cosine   DOUBLE PRECISION,
    audit_sampled   BOOLEAN,
    audit_cosine    DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_see_timestamp
    ON search_embed_event (timestamp);

CREATE INDEX IF NOT EXISTS idx_see_correlation_id
    ON search_embed_event (correlation_id);
