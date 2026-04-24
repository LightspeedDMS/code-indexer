---
name: admin_logs_query
category: admin
required_permission: manage_users
tl_dr: Query operational logs from SQLite database with pagination and filtering.
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

Query operational logs from SQLite database with pagination and filtering. USE CASES: (1) View recent server logs, (2) Search for specific errors/events, (3) Trace requests by correlation_id, (4) Filter by log level. RETURNS: Paginated array of log entries with timestamp, level, source, message, correlation_id, user_id, request_path. PERMISSIONS: Requires admin role (admin only). EXAMPLE: {"page": 1, "page_size": 50, "search": "SSO", "level": "ERROR"}