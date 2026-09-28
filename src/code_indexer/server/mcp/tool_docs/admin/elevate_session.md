---
name: elevate_session
category: admin
required_permission: query_repos
tl_dr: Submit a TOTP or recovery code to open a step-up elevation window for this session.
slim_description: "Open a TOTP step-up elevation window for the current session by submitting totp_code (6 digits) or recovery_code. Required before tools that return elevation_required. Available to every TOTP-enrolled user."
inputSchema:
  type: object
  properties:
    totp_code:
      type: string
      description: "Current 6-digit code from the user's authenticator app. Provide this OR recovery_code, not both."
    recovery_code:
      type: string
      description: "One-time TOTP recovery code. Opens a limited 'totp_repair' window. Provide this OR totp_code, not both."
  required: []
  additionalProperties: false
outputSchema:
  type: object
  properties:
    elevated:
      type: boolean
      description: True when the elevation window was opened
    scope:
      type: string
      description: "'full' for a TOTP code, 'totp_repair' for a recovery code"
    elevated_until:
      type: number
      description: Unix timestamp when the window expires if idle
    max_until:
      type: number
      description: Unix timestamp of the absolute window expiry
    error:
      type: string
      description: Error code when the window was not opened
    setup_url:
      type: string
      description: TOTP setup page, returned with totp_setup_required
    message:
      type: string
      description: Human-readable error detail
---

TL;DR: Open a TOTP step-up elevation window for the current MCP session. The MCP equivalent of REST `POST /auth/elevate`.

WHEN TO USE:
Sensitive tools return `elevation_required` when TOTP elevation enforcement is on and the session has no active elevation window. Ask the user for the current code from their authenticator app, call `elevate_session(totp_code="123456")`, then retry the original tool.

WHO CAN USE IT:
Every authenticated user with TOTP enrolled. Elevation opens a window for the caller's own account only; it never grants a role or permission the caller does not already hold.

SESSION BINDING:
The window is bound to the credential that opened it (the login token's id for Bearer JWT sessions). A different token, even for the same user, needs its own elevation. MCP-credential and OAuth sessions are elevated automatically when TOTP is enrolled.

PARAMETERS:
- totp_code: current 6-digit code (opens a 'full' window)
- recovery_code: one-time recovery code (opens a 'totp_repair' window, enough only for TOTP repair actions)
Provide exactly one of the two.

RETURNS (success):
{"elevated": true, "scope": "full", "elevated_until": 1767225600.0, "max_until": 1767227400.0}

ERRORS:
- missing_code / ambiguous_code: provide exactly one of totp_code or recovery_code
- totp_setup_required: TOTP is not enrolled for this account (setup_url provided)
- elevation_failed: the code is wrong or already used
- rate_limited: too many failed attempts, try again later
- missing_session_key: this credential type cannot hold an elevation window
- elevation_enforcement_disabled: step-up elevation is turned off by the operator, so no tool requires it
