---
name: authenticate
category: admin
required_permission: public
tl_dr: Authenticate with username and API key to establish session.
slim_description: "Authenticate with username and api_key (format: cidx_sk_...) to establish a session."
inputSchema:
  type: object
  properties:
    username:
      type: string
      description: Username
    api_key:
      type: string
      description: 'API key (format: cidx_sk_...)'
  required:
  - username
  - api_key
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether authentication succeeded
    message:
      type: string
      description: Human-readable status message (e.g. "Authentication successful")
    username:
      type: string
      description: Authenticated username (present on success)
    role:
      type: string
      description: Authenticated user's role (present on success)
    error:
      type: string
      description: Error message if failed
    retry_after:
      type: integer
      description: Seconds until the rate limiter allows another attempt (present only on rate-limit failure)
  required:
  - success
---

TL;DR: Authenticate with username and API key to establish session. The JWT session token is NEVER returned in the response body -- it is set as an HttpOnly `cidx_session` cookie on the HTTP response. WHEN TO USE: Required before using other tools on /mcp-public endpoint. WHEN NOT TO USE: Already authenticated. RELATED TOOLS: create_user (create new user account).