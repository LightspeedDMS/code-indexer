---
name: create_file
category: files
required_permission: repository:write
tl_dr: Create a new file in an activated repository.
---

Create a new file in an activated repository. USE CASES: (1) Create new source files, (2) Add configuration files, (3) Create documentation. REQUIREMENTS: Repository must be activated via activate_global_repo. File must not exist. RETURNS: File metadata including content_hash for future edits (optimistic locking). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/new_module.py", "content": "def hello():\n    return 'world'"}