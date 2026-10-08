---
name: admin_logs_export
category: admin
required_permission: manage_users
tl_dr: Export operational logs in JSON or CSV format for offline analysis or import.
slim_description: "Export operational logs in JSON or CSV format with optional filters (search, level, correlation_id)."
inputSchema:
  type: object
  properties:
    format:
      type: string
      description: 'Export format: ''json'' (default) or ''csv'''
      enum:
      - json
      - csv
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
    format:
      type: string
      description: Export format used (json or csv)
    count:
      type: integer
      description: Total number of logs exported
    data:
      type: string
      description: Exported log data as JSON string (with metadata) or CSV string (with BOM)
    filters:
      type: object
      description: Filters applied to export
      properties:
        search:
          type:
          - string
          - 'null'
        level:
          type:
          - string
          - 'null'
        correlation_id:
          type:
          - string
          - 'null'
  required:
  - success
  - format
  - count
  - data
  - filters
---

Export operational logs in JSON or CSV format for offline analysis or external tool import. USE CASES: (1) Download filtered logs for support tickets, (2) Import into spreadsheet or log analysis tools, (3) Share error logs with team, (4) Archive logs.

PERMISSIONS: Requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`).

RETURNS: `{"success": true, "format": "...", "count": N, "data": "...", "filters": {"search", "level", "correlation_id"}}`. Every log matching the filters is included, newest first, in one response: there is no pagination and no size limit, so narrow the filters on a busy server. For `json`, `data` is a JSON string `{"metadata": {"exported_at", "filters", "count"}, "logs": [...]}`; for `csv`, `data` is CSV text with a UTF-8 BOM.

LIMITATION: On the SQLite log store (solo mode), a database read error is logged on the server and the export is returned as `success: true` with `count: 0`. A zero-count export is therefore not proof that no logs matched; cross-check with admin_logs_query.

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on)
- `Invalid format '<format>'. Must be 'json' or 'csv'.`
- `Log database not configured`

EXAMPLE: {"format": "json", "search": "OAuth", "level": "ERROR"}
