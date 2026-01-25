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

TL;DR: Create a user-specific repository workspace for editing files, working on non-default branches, or combining multiple repos into a composite.

USE CASES:
(1) Work on a non-default branch (e.g., feature branch or release branch)
(2) Create a composite repository searching across multiple repos (frontend + backend + shared)
(3) Set up an editable workspace for file CRUD and git write operations

WHEN TO USE vs WHEN NOT TO USE:
- Use activate_repository: Need to edit files, commit changes, or work on non-default branch
- Skip activation: Just searching/reading code on default branch (use 'repo-global' directly)

WHAT IT DOES:
- Creates a user-specific repository workspace with custom alias
- Clones or references golden repository for your exclusive use
- Enables file CRUD and git write operations on your workspace
- Optionally combines multiple golden repos into single searchable composite

REQUIREMENTS:
- Permission: 'activate_repos' (power_user or admin role)
- Golden repository must exist (use list_global_repos to verify)
- For composites: all component golden repos must exist

PARAMETERS:
- golden_repo_alias: Single repo to activate (mutually exclusive with golden_repo_aliases)
- golden_repo_aliases: Array of repos for composite (mutually exclusive with golden_repo_alias)
- user_alias: Your custom name for this activation (optional, auto-generated if omitted)
- branch_name: Specific branch to check out (optional, uses default branch if omitted)

RETURNS:
{
  "success": true,
  "job_id": "abc-123",
  "message": "Activation started, use get_job_statistics to monitor progress"
}

EXAMPLE - Single repo:
activate_repository(golden_repo_alias='backend', user_alias='my-backend', branch_name='feature-auth')
-> Creates 'my-backend' workspace on feature-auth branch

EXAMPLE - Composite:
activate_repository(golden_repo_aliases=['frontend', 'backend', 'shared'], user_alias='fullstack')
-> Creates 'fullstack' composite searching across all 3 repos

COMMON ERRORS:
- "Permission denied" -> You need power_user or admin role
- "Golden repository not found" -> Use list_global_repos to verify alias
- "Branch does not exist" -> Check available branches with get_branches
- "Activation already exists" -> Use different user_alias or deactivate existing one first

TYPICAL WORKFLOW:
1. Find repo: list_global_repos()
2. Activate: activate_repository(golden_repo_alias='backend', user_alias='my-work')
3. Monitor: get_job_statistics() until active=0
4. Use: edit_file(repository_alias='my-work', ...)
5. Cleanup: deactivate_repository('my-work')

RELATED TOOLS:
- list_global_repos: See available golden repositories
- deactivate_repository: Remove activation when done
- manage_composite_repository: Modify composite after creation
- get_job_statistics: Monitor activation progress
