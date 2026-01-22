---
name: delete_file
category: files
required_permission: repository:write
tl_dr: Delete a file from an activated repository.
---

Delete a file from an activated repository. USE CASES: (1) Remove obsolete files, (2) Clean up temporary files, (3) Delete test fixtures. SAFETY: Optional content_hash validation prevents accidental deletion of modified files. REQUIREMENTS: File must exist. Repository must be activated. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/old_module.py", "content_hash": "xyz789"}