-- Migration 051: X-Ray graph-build K self-calibration samples
-- (Story #1787, S2 amendment, AC16).
--
-- Records (language, source_bytes, decls, call_sites, candidate_edges,
-- actual_peak_rss) per completed graph build so the per-language memory
-- multiplier K (AC12 Gate 1/Gate 2) can self-calibrate over time. This
-- table MUST be cluster-shared (not node-local): builds route to
-- arbitrary nodes via HAProxy, so a node-local K would let a repo that
-- OOM'd on one node keep OOM-ing on every other node independently.
--
-- get_k(language) is answered as MAX(actual_peak_rss / source_bytes)
-- for that language -- deliberately the WORST observed multiplier, not
-- the average, matching the conservative-by-design seed table
-- (k_seed_table.py). idx_xray_k_calibration_language exists to make
-- that per-language MAX query index-only.

CREATE TABLE IF NOT EXISTS xray_graph_k_calibration_samples (
    id               BIGSERIAL PRIMARY KEY,
    language         TEXT             NOT NULL,
    source_bytes     BIGINT           NOT NULL,
    decls            BIGINT           NOT NULL,
    call_sites       BIGINT           NOT NULL,
    candidate_edges  BIGINT           NOT NULL,
    actual_peak_rss  BIGINT           NOT NULL,
    recorded_at      DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_xray_k_calibration_language
ON xray_graph_k_calibration_samples (language);
