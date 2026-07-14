-- Migration 035: HNSW orphan repair fleet sweep state (Story #1360, Epic #1333 S3)
--
-- Durable per-cluster cursor + pass-stats for the paced, resumable fleet
-- sweep that walks ALL on-disk HNSW indexes (regular + temporal + multimodal)
-- detecting and repairing orphans in pre-existing indexes built before S2's
-- build-path self-heal fix existed.
--
-- Singleton row (id=1). last_completed_key is a STRING stable sort key
-- (e.g. "golden:myrepo:.code-indexer/index/voyage-code-3/hnsw_index.bin")
-- -- NEVER a numeric offset. A numeric position into a materialized
-- candidate list would silently mean a DIFFERENT item once temporal shards
-- / activated repos are created or removed between ticks; a string key
-- resumes correctly regardless of insertions/deletions in the candidate set.

CREATE TABLE IF NOT EXISTS hnsw_orphan_sweep_state (
    id                              INTEGER PRIMARY KEY,
    pass_id                         INTEGER NOT NULL DEFAULT 1,
    last_completed_key              TEXT,
    pass_started_at                 TIMESTAMPTZ,
    pass_indexes_checked            INTEGER NOT NULL DEFAULT 0,
    pass_orphaned_found             INTEGER NOT NULL DEFAULT 0,
    pass_repaired                   INTEGER NOT NULL DEFAULT 0,
    pass_errors                     INTEGER NOT NULL DEFAULT 0,
    pass_transient_skips            INTEGER NOT NULL DEFAULT 0,
    last_full_pass_completed_at     TIMESTAMPTZ,
    total_orphans_repaired_lifetime INTEGER NOT NULL DEFAULT 0,
    updated_at                      TIMESTAMPTZ
);
