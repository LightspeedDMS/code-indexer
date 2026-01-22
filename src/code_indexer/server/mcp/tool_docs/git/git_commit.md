---
name: git_commit
category: git
required_permission: repository:write
tl_dr: Create a commit with staged changes.
---

TL;DR: Create a commit with staged changes. Create a git commit with staged changes. USE CASES: (1) Commit staged files, (2) Create checkpoint with message, (3) Record changes with attribution. REQUIREMENTS: Must have staged files. OPTIONAL: author_name and author_email for custom commit attribution. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "message": "Fix authentication bug", "author_name": "John Doe", "author_email": "john@example.com"} Returns: {"success": true, "commit_hash": "abc123def...", "short_hash": "abc123d", "message": "Fix bug", "author": "John Doe", "files_committed": ["src/file.py"]}