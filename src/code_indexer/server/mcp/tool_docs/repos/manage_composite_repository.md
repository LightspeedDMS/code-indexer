---
name: manage_composite_repository
category: repos
required_permission: activate_repos
tl_dr: 'Perform operations on composite repositories (multi-repo activations).


  WHAT IS A COMPOSITE REPOSITORY:

  A composite repository is a virtual repository that combines multiple golden repositories
  into a single searchable unit.'
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
