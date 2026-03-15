---
name: merge_pull_request
category: git
required_permission: repository:write
tl_dr: Merge a GitHub pull request or GitLab merge request. Auto-detects forge type from the remote URL. Credentials are auto-fetched from stored git credentials.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    number:
      type: integer
      description: Pull/merge request number to merge
    merge_method:
      type: string
      description: 'Merge strategy: merge, squash, or rebase (default: merge)'
      default: merge
    commit_message:
      type: string
      description: Optional custom commit message for the merge commit
    delete_branch:
      type: boolean
      description: 'If true, delete the source branch after merging (default: false)'
      default: false
  required:
  - repository_alias
  - number
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    merged:
      type: boolean
      description: True if the PR/MR was merged
    sha:
      type: string
      description: Commit SHA of the merge commit
    message:
      type: string
      description: Confirmation message
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    error:
      type: string
      description: Error message on failure
---

TL;DR: Merge a GitHub pull request or GitLab merge request. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Merge a feature branch PR after review approval, (2) Squash-merge a PR to keep a clean history, (3) Merge and automatically delete the source branch. MERGE METHODS: 'merge' (default) creates a merge commit, 'squash' squashes all commits into one, 'rebase' replays commits on top of the target branch. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "number": 42, "merge_method": "squash", "delete_branch": true} Returns: {"success": true, "merged": true, "sha": "abc123", "message": "PR #42 merged", "forge_type": "github"}
