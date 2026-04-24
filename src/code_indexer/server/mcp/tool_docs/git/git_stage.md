---
name: git_stage
category: git
required_permission: repository:write
tl_dr: Stage files for commit (git add).
---

TL;DR: Stage files for commit (git add). Stage files for commit (git add). USE CASES: (1) Stage modified files, (2) Stage new files, (3) Prepare files for commit. REQUIREMENTS: Files must exist and have changes. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_paths": ["src/file1.py", "src/file2.py"]} Returns: {"success": true, "staged_files": ["src/file1.py", "src/file2.py"]}