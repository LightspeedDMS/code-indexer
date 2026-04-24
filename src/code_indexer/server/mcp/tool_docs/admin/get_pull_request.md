---
name: get_pull_request
category: admin
required_permission: query_repos
tl_dr: Get full details of a single GitHub pull request or GitLab merge request.
---

TL;DR: Get full details of a single GitHub pull request or GitLab merge request. The forge type is auto-detected from the repository's remote URL. Credentials are auto-fetched from stored git credentials. Returns description, labels, reviewers, mergeable status, CI status, and diff statistics. USE CASES: (1) Read the full body/description of a PR before commenting, (2) Check merge status and CI status before approving, (3) Inspect diff statistics to understand scope of change. NOTES: GitHub mergeable field can be null while GitHub calculates merge status. GitLab merge_status 'can_be_merged' maps to mergeable=true. CI status comes from head_pipeline.status on GitLab and mergeable_state on GitHub. EXAMPLE: {"repository_alias": "my-repo", "number": 42} Returns: {"success": true, "pull_request": {"number": 42, "title": "Add feature", "description": "This PR adds...", "state": "open", "author": "alice", "source_branch": "feature/new", "target_branch": "main", "url": "https://github.com/org/repo/pull/42", "labels": ["enhancement"], "reviewers": ["bob"], "mergeable": true, "ci_status": "clean", "diff_stats": {"additions": 150, "deletions": 30, "changed_files": 5}, "created_at": "2026-03-10T14:30:00Z", "updated_at": "2026-03-12T09:15:00Z"}, "forge_type": "github"}
