---
name: git_unstage
category: git
required_permission: repository:write
tl_dr: Remove files from staging area (git reset HEAD).
---

TL;DR: Remove files from staging area (git reset HEAD). Unstage files (git reset HEAD). USE CASES: (1) Remove files from staging area, (2) Un-stage accidentally staged files. REQUIREMENTS: Files must be currently staged. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_paths": ["src/file1.py"]} Returns: {"success": true, "unstaged_files": ["src/file1.py"]}