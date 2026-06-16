-- Migration 028: query-embedding cache table (Story #1105)
--
-- Stores float32 little-endian embedding blobs keyed by
-- (cache_key, provider, model, dimension).  Separate from the main server
-- tables to isolate large BLOB writes.
--
-- INVARIANT: this table stores ONLY query-purpose embeddings.  NEVER write
-- document-purpose embeddings here -- the two use different Cohere input_type
-- semantics (search_query vs search_document) and are not interchangeable.

CREATE TABLE IF NOT EXISTS query_embedding_cache (
    cache_key  TEXT             NOT NULL,
    provider   TEXT             NOT NULL,
    model      TEXT             NOT NULL,
    dimension  INTEGER          NOT NULL,
    embedding  BYTEA            NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    last_used  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (cache_key, provider, model, dimension)
);

CREATE INDEX IF NOT EXISTS idx_qec_last_used
    ON query_embedding_cache (last_used);
