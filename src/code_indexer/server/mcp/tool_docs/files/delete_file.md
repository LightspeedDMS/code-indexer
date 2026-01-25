---
name: delete_file
category: files
required_permission: repository:write
tl_dr: Delete a file from an activated repository.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    file_path:
      type: string
      description: Path to file to delete
    content_hash:
      type: string
      description: Optional SHA-256 hash for validation before delete (prevents accidental deletion of modified files)
  required:
  - repository_alias
  - file_path
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation success status
    file_path:
      type: string
      description: Deleted file path
    deleted_at:
      type: string
      description: Deletion timestamp (ISO 8601)
  required:
  - success
  - file_path
  - deleted_at
---

Delete a file from an activated repository. USE CASES: (1) Remove obsolete files, (2) Clean up temporary files, (3) Delete test fixtures. SAFETY: Optional content_hash validation prevents accidental deletion of modified files. REQUIREMENTS: File must exist. Repository must be activated. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/old_module.py", "content_hash": "xyz789"}