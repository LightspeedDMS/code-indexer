---
name: admin_logs_query
category: admin
required_permission: manage_users
tl_dr: Query the server's operational logs with pagination and filtering.
slim_description: "Query paginated operational logs with optional filters for search text, log level, and correlation_id. Each entry includes trace_id/span_id for OTEL trace correlation."
inputSchema:
  type: object
  properties:
    page:
      type: integer
      description: Page number (1-indexed, default 1)
    page_size:
      type: integer
      description: Number of logs per page (default 50, max 1000)
    sort_order:
      type: string
      description: 'Sort order: ''asc'' (oldest first) or ''desc'' (newest first, default)'
      enum:
      - asc
      - desc
    search:
      type: string
      description: Text search across message and correlation_id (case-insensitive)
    level:
      type: string
      description: Filter by log level(s), comma-separated (e.g., 'ERROR' or 'ERROR,WARNING')
    correlation_id:
      type: string
      description: Filter by exact correlation ID
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation success status
    logs:
      type: array
      description: Array of log entries
      items:
        type: object
        properties:
          id:
            type: integer
          timestamp:
            type: string
          level:
            type: string
          source:
            type: string
          message:
            type: string
          correlation_id:
            type:
            - string
            - 'null'
          trace_id:
            type: string
            description: OTEL trace ID (32-char hex, or "0"*32 zero-value when no span was active)
          span_id:
            type: string
            description: OTEL span ID (16-char hex, or "0"*16 zero-value when no span was active)
          user_id:
            type:
            - string
            - 'null'
          request_path:
            type:
            - string
            - 'null'
    pagination:
      type: object
      description: Pagination metadata
      properties:
        page:
          type: integer
        page_size:
          type: integer
        total:
          type: integer
        total_pages:
          type: integer
  required:
  - success
  - logs
  - pagination
---

Query the server's operational logs with pagination and filtering. Logs are read from the same store the server writes them to: the SQLite log database in solo mode, the shared database in cluster mode. `page_size` values above 1000 are reduced to 1000; zero or negative values use the default of 50. USE CASES: (1) View recent server logs, (2) Search for specific errors/events, (3) Trace requests by correlation_id, (4) Filter by log level, (5) Jump from a log line to its OTEL trace via trace_id/span_id. RETURNS: Paginated array of log entries with timestamp, level, source, message, correlation_id, trace_id, span_id, user_id, request_path. trace_id/span_id are always populated -- real hex IDs when the log occurred inside an active OTEL span, or the documented zero-values ("0"*32 / "0"*16) when telemetry was disabled or no span was active. PERMISSIONS: Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`).

ERRORS:
- `Permission denied: admin role required` (`{"success": false, "error": "..."}`)
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
- `Log database not configured`

EXAMPLE: {"page": 1, "page_size": 50, "search": "SSO", "level": "ERROR"}
