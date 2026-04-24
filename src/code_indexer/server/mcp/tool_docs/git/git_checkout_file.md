---
name: git_checkout_file
category: git
required_permission: repository:write
tl_dr: Discard local changes and restore file to HEAD.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    file_path:
      type: string
      description: File path to restore (relative to repository root)
  required:
  - repository_alias
  - file_path
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    restored_files:
      type: array
      items:
        type: string
      description: List of files restored to HEAD state
---

TL;DR: Discard local changes and restore file to HEAD. Restore file to HEAD version (discard local changes). USE CASES: (1) Discard unwanted changes, (2) Restore deleted file, (3) Reset file to last commit. SAFETY: This discards local modifications to the file. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/file.py"} Returns: {"success": true, "restored_files": ["src/file.py"]}