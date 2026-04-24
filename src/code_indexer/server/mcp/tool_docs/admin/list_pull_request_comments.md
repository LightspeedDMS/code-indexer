---
name: list_pull_request_comments
category: admin
required_permission: query_repos
tl_dr: Read all comments on a GitHub pull request or GitLab merge request.
---

TL;DR: Read all comments on a GitHub pull request or GitLab merge request. Returns both inline review comments (attached to specific lines) and general conversation comments in a single unified list sorted by created_at. Forge type is auto-detected from the repository remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Read all feedback on a PR before addressing review comments, (2) Check if inline review threads are resolved, (3) Get full conversation history for a PR/MR. GITHUB: Merges two API endpoints - pull request review comments (inline) and issue comments (general). resolved field is always null (not available in GitHub API). GITLAB: Fetches merge request notes, filters out system notes (status changes, assignments). resolved field reflects GitLab resolvable/resolved fields. EXAMPLE: {"repository_alias": "my-repo", "number": 42, "limit": 50} Returns: {"success": true, "comments": [{"id": 101, "author": "reviewer1", "body": "This needs error handling", "created_at": "2026-03-11T10:00:00Z", "updated_at": "2026-03-11T10:00:00Z", "file_path": "src/auth.py", "line_number": 42, "is_review_comment": true, "resolved": false}], "forge_type": "github", "count": 1}
