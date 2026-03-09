---
name: create_pull_request
category: git
required_permission: repository:write
tl_dr: Create a GitHub pull request or GitLab merge request from a repository in write mode. Auto-detects forge type (github/gitlab) from the remote URL. Requires write mode to be active.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias (must be in write mode)
    title:
      type: string
      description: Pull/merge request title
    body:
      type: string
      description: 'Pull/merge request description (default: empty string)'
      default: ''
    head:
      type: string
      description: Source branch name (the branch with your changes)
    base:
      type: string
      description: Target branch name (the branch to merge into)
    token:
      type: string
      description: Personal access token for the forge API (GitHub PAT or GitLab PAT)
  required:
  - repository_alias
  - title
  - head
  - base
  - token
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    pr_url:
      type: string
      description: URL of the created pull/merge request
    pr_number:
      type: integer
      description: Number of the created pull/merge request
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    error:
      type: string
      description: Error message on failure
---

TL;DR: Create a GitHub pull request or GitLab merge request. The forge type is auto-detected from the repository's remote URL. REQUIRES write mode to be active. USE CASES: (1) Open a PR/MR after committing and pushing changes, (2) Create review requests for feature branches. WORKFLOW: enter_write_mode -> create_file/edit_file -> git_stage -> git_commit -> git_push -> create_pull_request -> exit_write_mode. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "title": "Add new feature", "body": "Implements...", "head": "feature/my-branch", "base": "main", "token": "ghp_..."} Returns: {"success": true, "pr_url": "https://github.com/org/repo/pull/42", "pr_number": 42, "forge_type": "github"}
