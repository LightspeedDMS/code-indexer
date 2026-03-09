---
name: configure_git_credential
category: admin
required_permission: query_repos
tl_dr: Configure a git forge personal access token with identity discovery.
inputSchema:
  type: object
  properties:
    forge_type:
      type: string
      description: Git forge type - "github" or "gitlab"
    forge_host:
      type: string
      description: Forge hostname (e.g., "github.com", "gitlab.com", "github.corp.com")
    token:
      type: string
      description: Personal access token for the forge
    name:
      type: string
      description: Optional human-readable label for this credential
  required: [forge_type, forge_host, token]
---

TL;DR: Configure a git forge PAT. Validates the token against the forge API, discovers your identity (name, email, username), and stores the token encrypted.

USE CASES:
- Set up GitHub/GitLab PAT for push operations
- Configure credentials for GitHub Enterprise or self-hosted GitLab
- Update an existing credential with a new token

INPUTS:
- forge_type (required): "github" or "gitlab"
- forge_host (required): Hostname of the forge (e.g., "github.com")
- token (required): Your personal access token
- name (optional): Label for this credential (e.g., "Work GitHub")

RETURNS:
- credential_id: Unique identifier for the stored credential
- forge_username: Your username on the forge
- git_user_name: Your display name from the forge
- git_user_email: Your email from the forge

SECURITY: Token is validated against the forge API before storage. Stored with AES-256-CBC encryption.

EXAMPLE: {"forge_type": "github", "forge_host": "github.com", "token": "ghp_xxx", "name": "My GitHub"} Returns: {"success": true, "credential_id": "uuid", "forge_username": "octocat"}
