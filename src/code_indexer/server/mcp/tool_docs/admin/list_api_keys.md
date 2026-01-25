---
name: list_api_keys
category: admin
required_permission: query_repos
tl_dr: List all API keys for the authenticated user.
inputSchema:
  type: object
  properties: {}
  required: []
---

List all API keys for the authenticated user. Returns key metadata (ID, description, created_at, last_used) but NOT the key values.

USE CASES:
- View your existing API keys
- Check when keys were last used
- Find key ID for deletion

RETURNS:
- keys: Array of key metadata objects

NOTE: Full key values are only shown once at creation time.