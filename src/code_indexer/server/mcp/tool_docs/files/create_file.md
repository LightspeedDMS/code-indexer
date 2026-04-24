---
name: create_file
category: files
required_permission: repository:write
tl_dr: Create a new file in an activated repository.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias (user workspace identifier)
    file_path:
      type: string
      description: Path to new file within repository (relative path)
      pattern: ^(?!.*\.git/).*$
    content:
      type: string
      description: File content (UTF-8 text)
  required:
  - repository_alias
  - file_path
  - content
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the file creation succeeded
    file_path:
      type: string
      description: Relative path to created file (present when success=true)
    content_hash:
      type: string
      description: SHA-256 hash of file content for optimistic locking in future edits (present when success=true)
    size_bytes:
      type: integer
      description: File size in bytes (present when success=true)
    created_at:
      type: string
      description: ISO 8601 timestamp when file was created (present when success=true)
    error:
      type: string
      description: Error message (present when success=false)
  required:
  - success
---

Create a new file in an activated repository. USE CASES: (1) Create new source files, (2) Add configuration files, (3) Create documentation. REQUIREMENTS: Repository must be activated via activate_global_repo. File must not exist. RETURNS: File metadata including content_hash for future edits (optimistic locking). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/new_module.py", "content": "def hello():\n    return 'world'"}