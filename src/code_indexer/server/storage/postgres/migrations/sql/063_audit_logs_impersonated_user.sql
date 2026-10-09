-- Audit records written during MCP impersonation name the authenticated
-- administrator as the actor (admin_id) and the impersonated user as the
-- subject (impersonated_user).  NULL outside impersonation.  The SQLite
-- schema (AuditLogService._ensure_schema) adds the same name and type.
--
-- Backward-compatible additive change only: nullable, ADD COLUMN IF NOT
-- EXISTS.  Rows written by older code keep NULL.

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS impersonated_user TEXT;
