---
name: query_audit_logs
category: admin
required_permission: manage_users
tl_dr: Query security audit logs with optional filtering (admin only).
slim_description: "Query security audit logs with filters (actor, action, target, outcome, source, IP, correlation id, UTC date range), tiers, cursor paging and an authentication-activity aggregate."
inputSchema:
  type: object
  properties:
    user:
      type: string
      description: Filter by the acting user (the admin_id column)
    action:
      type: string
      description: "Filter by exact action_type (exact, case-sensitive match; e.g. 'password_change_success', 'token_refresh_failure', 'pr_creation_success', 'git_cleanup', 'group_create'). Prefixes such as 'password_change' or 'pr_creation' do NOT match."
    action_type:
      type: string
      description: Same as action (either name may be used)
    target_type:
      type: string
      description: Filter by exact target type (e.g. 'user', 'group', 'repo', 'auth')
    target_id:
      type: string
      description: Filter by exact target id
    outcome:
      type: string
      enum: [success, failure, denied, attempted]
      description: Filter by outcome
    source:
      type: string
      enum: [rest, mcp, web, system]
      description: Filter by the front door the action came through
    ip_address:
      type: string
      description: Filter by exact client IP address
    correlation_id:
      type: string
      description: Filter by correlation id (every row written for one request shares it)
    from_date:
      type: string
      description: "Start of the UTC range, inclusive: YYYY-MM-DD or an ISO 8601 date-time (e.g. '2024-01-01' or '2024-01-01T08:00:00Z')"
    to_date:
      type: string
      description: "End of the UTC range, inclusive: YYYY-MM-DD (the whole day) or an ISO 8601 date-time"
    tier:
      type: string
      enum: [all, security, auth_activity]
      default: all
      description: "Which rows to read: all (default), security (every non-authentication row plus password changes, rate-limit trips, security incidents and impersonation) or auth_activity (routine logins, token refreshes and other authentication activity)"
    cursor:
      type: string
      description: Paging token from a previous response (next_cursor for older rows, prev_cursor with direction 'newer' for newer rows). Cannot be combined with page.
    direction:
      type: string
      enum: [older, newer]
      default: older
      description: Paging direction for cursor; 'newer' requires a cursor
    limit:
      type: integer
      description: Maximum number of entries to return
      default: 100
      minimum: 1
      maximum: 1000
    page:
      type: integer
      description: Page number for offset pagination (1-based); prefer cursor
      default: 1
      minimum: 1
    aggregate:
      type: boolean
      default: false
      description: With tier 'auth_activity', return counts grouped by action and outcome instead of rows (last 24 hours unless a date range or all_time is given)
    all_time:
      type: boolean
      default: false
      description: With aggregate, lift the default 24 hour window. Cannot be combined with a date range.
  required: []
---

Query the audit log with optional filtering (admin only). Requires MCP elevation (TOTP step-up) when elevation enforcement is on. The Web Audit Logs page, this tool and REST `GET /api/v1/audit-logs` read through the same query, so the same filters return the same rows, order, counts and paging tokens.

USE CASES:
- Investigate security incidents
- Review user authentication history
- Audit administrative actions
- Follow one request across every row it wrote (correlation_id)
- Monitor for suspicious activity

INPUTS:
- user (optional): Filter by the acting user
- action / action_type (optional): Filter by exact action_type (exact, case-sensitive match). Prefixes do NOT match.
- target_type, target_id, outcome, source, ip_address, correlation_id (optional): exact-match filters
- from_date / to_date (optional): UTC range, inclusive; a date alone covers the whole day
- tier (optional): all (default), security or auth_activity
- cursor / direction (optional): keyset paging tokens (see RETURNS); stable when rows share a timestamp
- limit (optional): Maximum number of entries to return (default: 100, maximum: 1000)
- page (optional): Offset page number (1-based, default: 1); bounded, prefer cursor
- aggregate / all_time (optional): authentication-activity counts, see above

RETURNS (rows):
- entries: newest first. Each entry has id, timestamp, admin_id (also as user), action_type (also as action), target_type, target_id, resource (the recorded pull request URL for PR-creation entries -- a plain web URL, also in details.pr_url -- otherwise target_id), outcome, pairing_state, submitted_only, source, ip_address, node_id, correlation_id, auth_method, actor_is_system, actor_is_authenticated, event_uuid, details and impersonated_user (the user an administrator was impersonating over MCP when the action was performed -- admin_id is then the administrator; null otherwise).
- details: the decoded object with only the fields a reader may see; the names of any other stored fields are listed under omitted_fields, and content that is not a JSON object appears as omitted_fields ["(unstructured)"]
- total: matching entries across every page, exact up to 10,000
- total_capped: true when more than 10,000 entries match (total then reads 10,000)
- next_cursor: token for the next (older) page, or null
- prev_cursor: token for the newer page (use with direction 'newer'), or null
- has_more: more entries exist in the requested direction

RETURNS (aggregate=true):
- entries: empty
- groups: action_type, outcome, count, first_seen, last_seen, distinct_actors, distinct_ips
- total: events in the returned groups
- window_from / window_to / all_time / truncated

PERMISSIONS: Requires manage_users (admin only).

ERRORS:
- success false with an error message: a bad argument (unknown tier, outcome, source or direction, malformed cursor, cursor combined with page, unparseable or inverted date range, aggregate outside tier auth_activity)
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
