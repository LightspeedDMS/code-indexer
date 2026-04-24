---
name: list_pull_requests
category: admin
required_permission: query_repos
tl_dr: List GitHub pull requests or GitLab merge requests for a repository.
---

TL;DR: List GitHub pull requests or GitLab merge requests for a repository. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) List open PRs awaiting review, (2) Find merged PRs by a specific author, (3) Check all PRs regardless of state. STATES: open (default) - active PRs/MRs, closed - closed without merge, merged - successfully merged, all - all states. NOTE: For GitHub, 'merged' state uses post-filtering on closed PRs (checks merged_at field). EXAMPLE: {"repository_alias": "my-repo", "state": "open", "limit": 10} Returns: {"success": true, "pull_requests": [{"number": 42, "title": "Add feature", "state": "open", "author": "alice", "source_branch": "feature/new", "target_branch": "main", "url": "https://github.com/org/repo/pull/42", "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z"}], "forge_type": "github", "count": 1}
