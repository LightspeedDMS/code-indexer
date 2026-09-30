-- Unified security audit log: attribution columns and indexes on audit_logs.
--
-- Every audit row records how it happened: the outcome, the front door it
-- entered through (source), the immediate peer address, the request
-- correlation id, the cluster node, how the caller authenticated, whether
-- the actor is a trusted server component, and a per-event uuid assigned when
-- the event is built (never at insert time).  The SQLite schema
-- (AuditLogService._ensure_schema) adds the same names and types.
--
-- Backward-compatible additive change only:
--   - ALTER TABLE ADD COLUMN IF NOT EXISTS   (no-op if already present)
--   - CREATE INDEX IF NOT EXISTS             (no-op if already present)
-- Rows written by older code keep NULL in the new columns (0 for
-- actor_is_system); older code keeps inserting the original seven columns.
--
-- audit_logs itself is created by 002_groups_access_schema.sql.

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS ip_address TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS node_id TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS auth_method TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_is_system INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS event_uuid TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_logs_admin_id ON audit_logs(admin_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_target_id ON audit_logs(target_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_target_type_timestamp ON audit_logs(target_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp_id ON audit_logs(timestamp DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_correlation_id ON audit_logs(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_event_uuid ON audit_logs(event_uuid);
