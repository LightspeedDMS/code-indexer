-- Node metrics table for cluster-aware dashboard (Story #492).
--
-- Stores periodic per-node system metric snapshots written by
-- NodeMetricsWriterService.  The dashboard reads get_latest_per_node()
-- to render the cluster health carousel without polling psutil directly
-- in the HTTP request path.

CREATE TABLE IF NOT EXISTS node_metrics (
    id                  SERIAL          PRIMARY KEY,
    node_id             TEXT            NOT NULL,
    node_ip             TEXT            NOT NULL,
    timestamp           TIMESTAMPTZ     NOT NULL,
    cpu_usage           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    memory_percent      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    memory_used_bytes   BIGINT          NOT NULL DEFAULT 0,
    process_rss_mb      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    index_memory_mb     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    swap_used_mb        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    swap_total_mb       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    disk_read_kb_s      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    disk_write_kb_s     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    net_rx_kb_s         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    net_tx_kb_s         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    volumes_json        JSONB            NOT NULL DEFAULT '[]',
    server_version      TEXT            NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_node_metrics_node_timestamp
    ON node_metrics (node_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_node_metrics_timestamp
    ON node_metrics (timestamp);
