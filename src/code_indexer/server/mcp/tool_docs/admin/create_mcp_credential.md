---
name: create_mcp_credential
category: admin
required_permission: query_repos
tl_dr: Create a new MCP credential for MCP client connections.
inputSchema:
  type: object
  properties:
    description:
      type: string
      description: Optional human-readable description for the credential
  required: []
---

TL;DR: Create a new MCP credential for MCP client connections. Returns the full credential (one-time display - save it immediately).

USE CASES:
- Generate new MCP credential for MCP client connections
- Create separate credentials for different environments

INPUTS:
- description (optional): Human-readable label for the credential

RETURNS:
- credential_id: Unique identifier for the credential
- credential: Full credential value (SAVE THIS - shown only once)

SECURITY: The full credential is returned only at creation. Store it securely.

EXAMPLE: {"description": "Dev environment"} Returns: {"success": true, "credential_id": "cred_abc", "credential": "mcp_sk_xyz..."}