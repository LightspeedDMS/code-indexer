-- Migration 010: Centralized runtime configuration for cluster mode.
-- Story #578: Runtime config stored in PostgreSQL so Web UI changes
-- propagate to all cluster nodes.
-- Backward compatible: new table only, no modifications to existing schema.

CREATE TABLE IF NOT EXISTS server_config (
    config_key TEXT PRIMARY KEY DEFAULT 'runtime',
    config_json JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_by TEXT
);
