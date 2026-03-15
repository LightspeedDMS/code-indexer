---
name: get_pull_request
category: git
required_permission: query_repos
tl_dr: Get full details of a pull request / merge request including description, labels, reviewers, CI status, merge status, and diff statistics.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    number:
      type: integer
      description: Pull request / merge request number
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
    pull_request:
      type: object
      description: Normalized PR/MR object with full details
      properties:
        number:
          type: integer
          description: PR/MR number
        title:
          type: string
          description: PR/MR title
        description:
          type: string
          description: PR/MR body text
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
        labels:
          type: array
          description: List of label names
          items:
            type: string
        reviewers:
          type: array
          description: List of requested reviewer usernames
          items:
            type: string
        mergeable:
          description: 'Whether the PR/MR can be merged: true, false, or null (pending)'
        ci_status:
          type: string
          description: 'CI/pipeline status string (e.g. success, failed, pending)'
          nullable: true
        diff_stats:
          type: object
          description: Diff statistics
          properties:
            additions:
              type: integer
            deletions:
              type: integer
            changed_files:
              type: integer
        created_at:
          type: string
          description: Creation timestamp
        updated_at:
          type: string
          description: Last update timestamp
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    error:
      type: string
      description: Error message on failure
---

TL;DR: Get full details of a single GitHub pull request or GitLab merge request. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. Returns description, labels, reviewers, mergeable status, CI status, and diff statistics. USE CASES: (1) Read the full body/description of a PR before commenting, (2) Check merge status and CI status before approving, (3) Inspect diff statistics to understand scope of change. NOTES: GitHub mergeable field can be null while GitHub calculates merge status. GitLab merge_status 'can_be_merged' maps to mergeable=true. CI status comes from head_pipeline.status on GitLab and mergeable_state on GitHub. EXAMPLE: {"repository_alias": "my-repo", "number": 42} Returns: {"success": true, "pull_request": {"number": 42, "title": "Add feature", "description": "This PR adds...", "state": "open", "author": "alice", "source_branch": "feature/new", "target_branch": "main", "url": "https://github.com/org/repo/pull/42", "labels": ["enhancement"], "reviewers": ["bob"], "mergeable": true, "ci_status": "clean", "diff_stats": {"additions": 150, "deletions": 30, "changed_files": 5}, "created_at": "2026-03-10T14:30:00Z", "updated_at": "2026-03-12T09:15:00Z"}, "forge_type": "github"}
