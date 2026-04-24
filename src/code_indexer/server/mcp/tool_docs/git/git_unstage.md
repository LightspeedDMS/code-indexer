---
name: git_unstage
category: git
required_permission: repository:write
tl_dr: Remove files from staging area (git reset HEAD).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    file_paths:
      type: array
      items:
        type: string
      description: Array of file paths to unstage
  required:
  - repository_alias
  - file_paths
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    unstaged_files:
      type: array
      items:
        type: string
      description: List of file paths that were unstaged
---

TL;DR: Remove files from staging area (git reset HEAD). Unstage files (git reset HEAD). USE CASES: (1) Remove files from staging area, (2) Un-stage accidentally staged files. REQUIREMENTS: Files must be currently staged. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_paths": ["src/file1.py"]} Returns: {"success": true, "unstaged_files": ["src/file1.py"]}