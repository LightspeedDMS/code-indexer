---
name: set_global_config
category: repos
required_permission: manage_golden_repos
tl_dr: Configure auto-refresh interval for ALL global repositories system-wide.
slim_description: "Set the auto-refresh interval in seconds (minimum 60) applied to all global repositories system-wide."
inputSchema:
  type: object
  properties:
    refresh_interval:
      type: integer
      description: Refresh interval in seconds
      minimum: 60
  required:
  - refresh_interval
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    status:
      type: string
      description: Operation status
    refresh_interval:
      type: integer
      description: Updated refresh interval in seconds
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Configure the auto-refresh interval for ALL global repositories system-wide. ADMIN ONLY: requires the admin role and, when elevation enforcement is on, an active elevation window (TOTP step-up via `elevate_session`). get_global_config has no elevation gate.

INPUT: `{"refresh_interval": 300}` sets a 5-minute interval. `refresh_interval` is in seconds, minimum 60, no maximum. The value is saved in the server's runtime configuration (the same setting as the golden-repos refresh interval in the Web UI config screen) and an audit entry is recorded.

EFFECT: Global repositories are refreshed (pull latest changes and re-index) at this interval. The setting applies to all global repositories; there are no per-repository intervals.

TYPICAL VALUES: 300 (5 min), 900 (15 min), 3600 (1 hour), 86400 (1 day). Lower intervals keep code fresher at the cost of more load and network traffic.

RETURNS: `{"success": true, "status": "updated", "refresh_interval": 300}`. Confirm with get_global_config.

ERRORS (returned as `{"success": false, "error": "..."}` except the elevation codes):
- `Permission denied: admin role required`
- `elevation_required` / `totp_setup_required` (only when elevation enforcement is on; `totp_setup_required` includes `setup_url`)
- `Missing required parameter: refresh_interval`
- `Refresh interval must be at least 60 seconds. Got: <N> seconds.`

RELATED TOOLS: get_global_config (check current interval), refresh_golden_repo (force immediate refresh without changing interval), repository_status (check when specific repo last refreshed).
