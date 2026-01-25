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

TL;DR: Perform operations on composite repositories (multi-repo activations). Manage repositories created from multiple golden repos as a single searchable unit.

USE CASES:
(1) Update component repositories in a composite (add/remove repos from the composite)
(2) Re-sync all component repos in a composite activation
(3) Rebuild composite indexes after component changes

WHAT IS A COMPOSITE REPOSITORY:
A composite repository is an activation that combines multiple golden repositories into a single searchable unit.
Example: Combine 'backend-golden', 'frontend-golden', 'shared-golden' into one composite 'fullstack' activation.
Queries against the composite search across all component repositories.

WHAT IT DOES:
- Add or remove component repositories from existing composite
- Re-sync all components with their golden sources
- Rebuild composite indexes (necessary after component changes)
- View component repository status within composite

REQUIREMENTS:
- Permission: 'activate_repos' (power_user or admin role)
- Composite repository must already exist (created via activate_repository with golden_repo_aliases array)
- Component golden repositories must exist

PARAMETERS:
- user_alias: Your alias for the composite repository
- operation: String, one of:
  - 'create': Create new composite repository
  - 'update': Modify composite components
  - 'delete': Remove composite repository
- golden_repo_aliases: Array of golden repo aliases (required for create/update operations)

RETURNS:
{
  "success": true,
  "composite_alias": "fullstack",
  "operation": "update",
  "components": ["backend-golden", "frontend-golden", "shared-golden"],
  "reindex_job_id": "xyz789"  // if reindex triggered
}

EXAMPLE:
manage_composite_repository(
  user_alias='fullstack',
  operation='update',
  golden_repo_aliases=['backend-golden', 'frontend-golden', 'api-golden']
)
-> Updates 'fullstack' composite to include 'api-golden', triggers re-indexing

COMMON ERRORS:
- "Composite not found" -> Check alias with list_activated_repos()
- "Not a composite repository" -> Alias points to single-repo activation
- "Component already exists" -> Golden repo already in composite
- "Cannot remove last component" -> Composites need at least 2 components

TYPICAL WORKFLOW:
1. Create composite: manage_composite_repository(user_alias='fullstack', operation='create', golden_repo_aliases=['backend-golden', 'frontend-golden'])
2. Later add component: manage_composite_repository(user_alias='fullstack', operation='update', golden_repo_aliases=['backend-golden', 'frontend-golden', 'shared-golden'])
3. Delete composite: manage_composite_repository(user_alias='fullstack', operation='delete')

RELATED TOOLS:
- activate_repository: Create composite (pass array to golden_repo_aliases)
- list_activated_repos: See all your composites
- deactivate_repository: Remove entire composite
