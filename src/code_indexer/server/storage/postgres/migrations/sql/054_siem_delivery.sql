-- SIEM delivery (Google SecOps): delivery queue, batches, fleet state,
-- per-process readiness, destinations and backlog samples.
--
-- The SQLite schema (services/siem_delivery/db.py, created in groups.db
-- beside audit_logs) uses the same table, column and index names.
--
-- Backward-compatible additive change only:
--   - CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
--   - one seed row for the single-row state table (ON CONFLICT DO NOTHING)

CREATE TABLE IF NOT EXISTS siem_delivery_queue (
    id BIGSERIAL PRIMARY KEY,
    event_uuid TEXT NOT NULL UNIQUE,
    destination_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    action_type TEXT NOT NULL,
    event_payload TEXT,
    projection_error TEXT,
    status TEXT NOT NULL,
    batch_id TEXT,
    batch_ordinal INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    mapping_version INTEGER NOT NULL,
    quarantine_reason TEXT,
    quarantine_signature TEXT,
    boundary_kind TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    delivered_via TEXT
);

CREATE TABLE IF NOT EXISTS siem_delivery_batches (
    batch_id TEXT PRIMARY KEY,
    destination_key TEXT NOT NULL,
    body BYTEA,
    body_sha256 TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    mapping_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    last_class TEXT,
    lease_owner TEXT,
    lease_token BIGINT,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    send_attempts INTEGER NOT NULL DEFAULT 0,
    first_sent_at TIMESTAMPTZ,
    last_sent_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    outcome_unknown INTEGER NOT NULL DEFAULT 0,
    prior_outcome_unknown INTEGER NOT NULL DEFAULT 0,
    bisect_depth INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS siem_delivery_state (
    id INTEGER PRIMARY KEY,
    fence_counter BIGINT NOT NULL DEFAULT 0,
    halted_class TEXT,
    halted_signature TEXT,
    halted_since TIMESTAMPTZ,
    halted_batch_id TEXT,
    halted_mapping_version INTEGER,
    next_probe_at TIMESTAMPTZ,
    canary_run_id TEXT,
    canary_destination_key TEXT,
    canary_mapping_version INTEGER,
    canary_expected TEXT,
    canary_sent_at TIMESTAMPTZ,
    canary_result TEXT,
    canary_result_signature TEXT,
    canary_actor TEXT,
    canary_confirmed_ids TEXT,
    canary_missing_action_types TEXT,
    canary_visible_confirmed_by TEXT,
    canary_visible_confirmed_at TIMESTAMPTZ,
    armed_destination_key TEXT,
    armed_at TIMESTAMPTZ,
    armed_config_version INTEGER,
    seen_config_version INTEGER NOT NULL DEFAULT 0,
    quarantine_window_start TIMESTAMPTZ,
    quarantine_window_count INTEGER NOT NULL DEFAULT 0,
    delivered_total BIGINT NOT NULL DEFAULT 0,
    resent_after_unknown_outcome BIGINT NOT NULL DEFAULT 0,
    unrecoverable_total BIGINT NOT NULL DEFAULT 0,
    capture_after_boundary_total BIGINT NOT NULL DEFAULT 0,
    capture_after_boundary_late_total BIGINT NOT NULL DEFAULT 0,
    boundary_settled_after_total BIGINT NOT NULL DEFAULT 0,
    boundary_settled_late_total BIGINT NOT NULL DEFAULT 0,
    last_boundary_scanned_id BIGINT NOT NULL DEFAULT 0,
    requeued_mapping_version INTEGER NOT NULL DEFAULT 0,
    stats_json TEXT,
    stats_refreshed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS siem_process_status (
    process_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    destination_key TEXT,
    probe_result TEXT NOT NULL,
    probed_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS siem_destinations (
    destination_key TEXT PRIMARY KEY,
    region TEXT,
    project_id TEXT,
    location TEXT,
    instance_id TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS siem_backlog_samples (
    sampled_at TIMESTAMPTZ PRIMARY KEY,
    backlog_rows_estimate BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_siem_queue_status_dest_id ON siem_delivery_queue(status, destination_key, id, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_siem_queue_status_id ON siem_delivery_queue(status, id);
CREATE INDEX IF NOT EXISTS idx_siem_queue_dest_created ON siem_delivery_queue(destination_key, created_at);
CREATE INDEX IF NOT EXISTS idx_siem_queue_boundary ON siem_delivery_queue(boundary_kind, id);
CREATE INDEX IF NOT EXISTS idx_siem_queue_status_created ON siem_delivery_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_siem_queue_status_delivered ON siem_delivery_queue(status, delivered_at);
CREATE INDEX IF NOT EXISTS idx_siem_queue_batch ON siem_delivery_queue(batch_id, batch_ordinal);
CREATE INDEX IF NOT EXISTS idx_siem_batches_dest_state_next ON siem_delivery_batches(destination_key, state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_siem_batches_state_created ON siem_delivery_batches(state, created_at);
CREATE INDEX IF NOT EXISTS idx_siem_process_expires ON siem_process_status(expires_at);
CREATE INDEX IF NOT EXISTS idx_siem_process_node_expires ON siem_process_status(node_id, expires_at);

INSERT INTO siem_delivery_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
