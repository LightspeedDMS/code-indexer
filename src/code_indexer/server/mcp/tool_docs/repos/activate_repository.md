---
name: activate_repository
category: repos
required_permission: activate_repos
tl_dr: Create a user-specific repository workspace for editing files, working on non-default branches, or combining multiple
  repos into a composite.
inputSchema:
  type: object
  properties:
    golden_repo_alias:
      type: string
      description: Golden repository alias (for single repo)
    golden_repo_aliases:
      type: array
      items:
        type: string
      description: Multiple golden repos (for composite)
    branch_name:
      type: string
      description: Branch to activate (optional)
    user_alias:
      type: string
      description: User-defined alias (optional)
  required: []
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
      description: Background job ID for tracking activation progress
    message:
      type: string
      description: Human-readable status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Create a user-specific repository workspace for editing files, working on non-default branches, or combining multiple repos into a composite.

USE CASES:
(1) Work on a non-default branch (e.g., feature branch or release branch)
(2) Create a composite repository searching across multiple repos (frontend + backend + shared)
(3) Set up an editable workspace for file CRUD and git write operations

WHAT IT DOES:
Creates a user-specific repository workspace with custom alias. Clones or references golden repository for your exclusive use. Enables file CRUD and git write operations. Optionally combines multiple golden repos into single searchable composite.

PARAMETER MUTUAL EXCLUSIVITY:
Only ONE of the following at a time:
- golden_repo_alias: Single repo to activate (mutually exclusive with golden_repo_aliases)
- golden_repo_aliases: Array of repos for composite (mutually exclusive with golden_repo_alias)

WORKFLOW:
1. Find available repo: list_global_repos()
2. Activate with custom alias: activate_repository(golden_repo_alias='backend', user_alias='my-work')
3. Monitor progress: get_job_statistics() until active=0
4. Use your workspace: edit_file(repository_alias='my-work', ...)
5. Cleanup when done: deactivate_repository('my-work')
