---
name: activate_repository
category: repos
required_permission: activate_repos
tl_dr: Create user workspace for editing files, non-default branches, or composites.
slim_description: "Create a user-specific workspace for editing files, working on non-default branches, or combining multiple repos into a composite. Pass exactly one of golden_repo_alias or golden_repo_aliases."
inputSchema:
  type: object
  properties:
    golden_repo_alias:
      type: string
      description: Golden repository alias without the -global suffix, for a single-repository workspace. Pass this or golden_repo_aliases, not both.
    golden_repo_aliases:
      type: array
      items:
        type: string
      description: Two or more golden repository aliases for a composite workspace. Pass this or golden_repo_alias, not both.
    branch_name:
      type: string
      description: Branch to activate (optional; defaults to the golden repository's default branch)
    user_alias:
      type: string
      description: Alias for the new workspace (optional; defaults to the golden repository alias)
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
Starts a background job that creates a workspace owned by the caller, under `user_alias`, from one golden repository or (composite) from several. The workspace supports file CRUD and git write operations. The call returns a `job_id` immediately: `{"success": true, "job_id": "...", "message": "Repository activation started"}`.

PARAMETERS:
Exactly one of these is required (the schema cannot express this, so the handler enforces it):
- golden_repo_alias: One golden repository alias, without the `-global` suffix (for example `example-repo`).
- golden_repo_aliases: Array of at least two golden repository aliases for a composite workspace.
Optional:
- branch_name: Defaults to the golden repository's default branch.
- user_alias: Defaults to the golden repository alias.

When group-based access control is configured, every requested golden repository must be accessible to the caller.

ERRORS (returned as `{"success": false, "error": "...", "job_id": null}`):
- `Missing required parameter: golden_repo_alias or golden_repo_aliases`
- `golden_repo_alias must be a string` / `golden_repo_aliases must be a list of non-empty strings`
- `Cannot specify both golden_repo_alias and golden_repo_aliases`
- `Composite activation requires at least 2 repositories`
- `Repository not accessible. Contact your administrator for access.`

WORKFLOW:
1. Find an available repository: list_global_repos() (use its `repo_name`, the alias without `-global`)
2. Activate with a custom alias: activate_repository(golden_repo_alias='example-repo', user_alias='my-work')
3. Track the returned job: get_job_details(job_id='<job_id>')
4. Use the workspace: edit_file(repository_alias='my-work', ...)
5. Clean up when done: deactivate_repository(user_alias='my-work')
