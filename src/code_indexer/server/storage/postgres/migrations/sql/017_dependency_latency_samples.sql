-- Story #680: External Dependency Latency Observability
-- Stores raw per-request latency samples for windowed percentile computation.
-- Backward-compatible: CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS
-- are idempotent and safe for rolling upgrades.
CREATE TABLE IF NOT EXISTS dependency_latency_samples (
    id BIGSERIAL PRIMARY KEY,
    node_id TEXT NOT NULL,
    dependency_name TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    latency_ms DOUBLE PRECISION NOT NULL,
    status_code INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dependency_latency_dep_timestamp
ON dependency_latency_samples(dependency_name, timestamp);
