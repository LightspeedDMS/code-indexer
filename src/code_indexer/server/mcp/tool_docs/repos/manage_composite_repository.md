---
name: manage_composite_repository
category: repos
required_permission: activate_repos
tl_dr: Perform operations on composite repositories (multi-repo activations).
slim_description: "Create, update, or delete a composite repository that combines multiple golden repositories into a single searchable unit."
inputSchema:
  type: object
  properties:
    operation:
      type: string
      description: Operation type
      enum:
      - create
      - update
      - delete
    user_alias:
      type: string
      description: Composite repository alias
    golden_repo_aliases:
      type: array
      items:
        type: string
      description: Golden repository aliases
  required:
  - operation
  - user_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    job_id:
      type:
      - string
      - 'null'
      description: Background job ID
    message:
      type: string
      description: Status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Perform operations on composite repositories (multi-repo activations).

WHAT IS A COMPOSITE REPOSITORY:
A composite repository is a virtual repository that combines multiple golden repositories into a single searchable unit. Example: Combine 'backend-repo', 'frontend-repo', 'shared-repo' into one composite 'fullstack' activation. Queries against the composite search across all component repositories simultaneously.

OPERATION TYPES:
- 'create': Activate a new composite from `golden_repo_aliases` (same as activate_repository with golden_repo_aliases)
- 'update': Replace the composite's component list: the handler submits a deactivation of the existing composite, then an activation of `user_alias` with the new `golden_repo_aliases` (pass the complete new list, not just the changes). A failure to deactivate is ignored and the activation is still attempted.
- 'delete': Deactivate the composite (same as deactivate_repository)

Each operation runs as a background job and returns `{"success": true, "job_id": "...", "message": "Composite repository '<alias>' creation|update|deletion started"}`; for 'update' the job_id is the activation job. Track it with get_job_details.

CRITICAL REQUIREMENT:
Composites must have at least 2 component repositories; create and update are rejected with `Composite activation requires at least 2 repositories` otherwise.

WARNING (update): the deactivation of the existing composite is submitted BEFORE the new list is checked. An update with fewer than 2 aliases is rejected, but removal of the existing composite has already been queued. Always pass at least 2 aliases to update.

ACCESS: When group-based access control is configured, every component alias (with or without '-global') must be accessible to the caller, otherwise `Access denied: repository '<alias>' is not accessible.`

PARAMETERS:
- user_alias: Your alias for the composite repository
- operation: One of 'create', 'update', 'delete'
- golden_repo_aliases: Array of golden repo aliases without '-global' (required for create/update)

ERRORS (`{"success": false, "error": "...", ...}`): `Missing required parameters: operation and user_alias`, `Unknown operation: <operation>`, the requirement and access errors above, and activation/deactivation failures.
