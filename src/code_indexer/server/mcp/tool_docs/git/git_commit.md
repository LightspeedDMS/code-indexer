---
name: git_commit
category: git
required_permission: repository:write
tl_dr: Create a commit with staged changes.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    message:
      type: string
      description: Commit message
    author_name:
      type: string
      description: Optional author name for commit attribution
    author_email:
      type: string
      description: Optional author email for commit attribution
  required:
  - repository_alias
  - message
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    commit_hash:
      type: string
      description: Full 40-character commit SHA
    short_hash:
      type: string
      description: 7-character short commit SHA
    message:
      type: string
      description: Commit message
    author:
      type: string
      description: Commit author
    files_committed:
      type: array
      items:
        type: string
      description: List of files included in commit
---

TL;DR: Create a commit with staged changes. Create a git commit with staged changes. USE CASES: (1) Commit staged files, (2) Create checkpoint with message, (3) Record changes with attribution. REQUIREMENTS: Must have staged files. OPTIONAL: author_name and author_email for custom commit attribution. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "message": "Fix authentication bug", "author_name": "John Doe", "author_email": "john@example.com"} Returns: {"success": true, "commit_hash": "abc123def...", "short_hash": "abc123d", "message": "Fix bug", "author": "John Doe", "files_committed": ["src/file.py"]}