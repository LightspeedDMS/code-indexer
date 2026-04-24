---
name: cs_list_repositories
category: admin
required_permission: delegate_open
tl_dr: List all repositories registered on Claude Server.
inputSchema:
  type: object
  properties: {}
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: True if the list was retrieved successfully
    repositories:
      type: array
      description: List of repositories registered on Claude Server
      items:
        type: object
        properties:
          name:
            type: string
          clone_status:
            type: string
            description: Current clone status (unknown/cloning/completed/failed)
          cidx_aware:
            type: boolean
          git_url:
            type: string
          branch:
            type: string
          current_branch:
            type: string
          registered_at:
            type: string
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
---

List all repositories registered on Claude Server, showing their current clone status and metadata.

REQUIRED PERMISSION: delegate_open (power_user or admin role)

BEHAVIOR:
Calls GET /repositories on Claude Server and returns all registered repositories with normalized field names.

CLONE STATUS VALUES:
- cloning: Repository is being cloned (not yet ready for delegation jobs)
- completed: Repository is ready for use in delegation jobs
- failed: Clone failed, repository cannot be used
- unknown: Status not yet determined

RESPONSE FIELDS (per repository):
- name: Repository name/alias
- clone_status: Current clone status (cloning, completed, failed, unknown)
- cidx_aware: Whether CIDX indexing is enabled for this repository
- git_url: Repository git URL
- branch: Target branch
- current_branch: Currently checked-out branch
- registered_at: When the repository was registered

ERRORS:
- 'Claude Delegation not configured' -> Delegation configuration not set up
- 'Access denied' -> User does not have delegate_open permission
- 'Failed to list repositories' -> Claude Server communication error
