---
name: manage_composite_repository
category: repos
required_permission: activate_repos
tl_dr: Perform operations on composite repositories (multi-repo activations).
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
A composite repository is a virtual repository that combines multiple golden repositories into a single searchable unit. Example: Combine 'backend-golden', 'frontend-golden', 'shared-golden' into one composite 'fullstack' activation. Queries against the composite search across all component repositories simultaneously.

OPERATION TYPES:
- 'create': Create new composite repository from multiple golden repos
- 'update': Add or remove component repositories from existing composite
- 'delete': Remove composite repository entirely

CRITICAL REQUIREMENT:
Composites must have at least 2 component repositories. Cannot create with 1 component, and cannot remove components if only 2 remain.

PARAMETERS:
- user_alias: Your alias for the composite repository
- operation: One of 'create', 'update', 'delete'
- golden_repo_aliases: Array of golden repo aliases (required for create/update)
