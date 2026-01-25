---
name: authenticate
category: admin
required_permission: public
tl_dr: Authenticate with username and API key to establish session.
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
    token:
      type: string
      description: JWT session token for subsequent requests
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Authenticate with username and API key to establish session. WHEN TO USE: Required before using other tools on /mcp-public endpoint. WHEN NOT TO USE: Already authenticated. RELATED TOOLS: create_user (create new user account).