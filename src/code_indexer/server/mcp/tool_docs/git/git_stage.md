---
name: git_stage
category: git
required_permission: repository:write
tl_dr: Stage files for commit (git add).
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
      description: Array of file paths to stage (relative to repository root)
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
    staged_files:
      type: array
      items:
        type: string
      description: List of file paths that were staged
---

TL;DR: Stage files for commit (git add). Stage files for commit (git add). USE CASES: (1) Stage modified files, (2) Stage new files, (3) Prepare files for commit. REQUIREMENTS: Files must exist and have changes. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_paths": ["src/file1.py", "src/file2.py"]} Returns: {"success": true, "staged_files": ["src/file1.py", "src/file2.py"]}