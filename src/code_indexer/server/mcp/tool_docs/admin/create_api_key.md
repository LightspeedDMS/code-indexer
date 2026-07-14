---
name: create_api_key
category: admin
required_permission: query_repos
tl_dr: Create a new API key for programmatic access.
slim_description: "Create a new API key for the authenticated user with an optional description."
inputSchema:
  type: object
  properties:
    description:
      type: string
      description: Optional human-readable description for the API key
  required: []
---

TL;DR: Create a new API key for programmatic access. Requires MCP elevation (TOTP step-up). Create a new API key for the authenticated user. Returns the full key value (one-time display - save it immediately).

USE CASES:
- Generate new API key for programmatic access
- Create separate keys for different applications

INPUTS:
- description (optional): Human-readable label for the key

RETURNS:
- key_id: Unique identifier for the key
- api_key: Full key value (SAVE THIS - shown only once)
- description: Key description

SECURITY: The full api_key is returned only at creation. Store it securely.

ERRORS:
- elevation_required: TOTP step-up needed
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)

EXAMPLE: {"description": "CI/CD automation"} Returns: {"success": true, "key_id": "key_xyz", "api_key": "cidx_sk_abc123...", "description": "CI/CD automation"}