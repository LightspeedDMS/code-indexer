---
name: admin_logs_export
category: admin
required_permission: manage_users
tl_dr: Export operational logs in JSON or CSV format for offline analysis or external tool import.
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

Export operational logs in JSON or CSV format for offline analysis or external tool import. USE CASES: (1) Download filtered logs for support tickets, (2) Import into Excel/log analysis tools, (3) Share error logs with team, (4) Archive logs. RETURNS: ALL logs matching filter criteria (no pagination) formatted as JSON or CSV. Includes export metadata with count and applied filters. PERMISSIONS: Requires admin role (admin only). EXAMPLE: {"format": "json", "search": "OAuth", "level": "ERROR"}