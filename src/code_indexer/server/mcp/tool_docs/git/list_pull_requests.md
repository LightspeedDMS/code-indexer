---
name: list_pull_requests
category: git
required_permission: query_repos
tl_dr: List pull requests / merge requests for a repository. Supports filtering by state (open/closed/merged/all) and author. Auto-detects forge type (github/gitlab) from remote URL.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    state:
      type: string
      description: 'Filter by state: open (default), closed, merged, all'
      default: open
      enum: [open, closed, merged, all]
    limit:
      type: integer
      description: 'Max results (default: 10, max: 100)'
      default: 10
      minimum: 1
      maximum: 100
    author:
      type: string
      description: Filter by author username (optional)
  required:
    - repository_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    pull_requests:
      type: array
      description: List of normalized PR/MR objects
      items:
        type: object
        properties:
          number:
            type: integer
            description: PR/MR number
          title:
            type: string
            description: PR/MR title
          state:
            type: string
            description: 'State: open, closed, or merged'
          author:
            type: string
            description: Author username
          source_branch:
            type: string
            description: Source branch name
          target_branch:
            type: string
            description: Target branch name
          url:
            type: string
            description: URL to the PR/MR
          created_at:
            type: string
            description: Creation timestamp
          updated_at:
            type: string
            description: Last update timestamp
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    count:
      type: integer
      description: Number of PR/MR items returned
    error:
      type: string
      description: Error message on failure
---

TL;DR: List GitHub pull requests or GitLab merge requests for a repository. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) List open PRs awaiting review, (2) Find merged PRs by a specific author, (3) Check all PRs regardless of state. STATES: open (default) - active PRs/MRs, closed - closed without merge, merged - successfully merged, all - all states. NOTE: For GitHub, 'merged' state uses post-filtering on closed PRs (checks merged_at field). EXAMPLE: {"repository_alias": "my-repo", "state": "open", "limit": 10} Returns: {"success": true, "pull_requests": [{"number": 42, "title": "Add feature", "state": "open", "author": "alice", "source_branch": "feature/new", "target_branch": "main", "url": "https://github.com/org/repo/pull/42", "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z"}], "forge_type": "github", "count": 1}
