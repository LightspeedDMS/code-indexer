---
name: cs_list_repositories
category: admin
required_permission: delegate_open
tl_dr: 'List all repositories registered on Claude Server, showing their current clone
  status and metadata.


  REQUIRED PERMISSION: delegate_open (power_user or admin role)


  BEHAVIOR:

  Calls GET /repositories on Claude Server and returns all registered repositories
  with normalized field names.


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

  - ''Claude Delegation not configured'' -> Delegation configuration not set up

  - ''Access denied'' -> User does not have delegate_open permission

  - ''Failed to list repositories'' -> Claude Server communication error.'
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
