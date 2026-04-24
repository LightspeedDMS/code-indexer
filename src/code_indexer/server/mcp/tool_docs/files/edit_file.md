---
name: edit_file
category: files
required_permission: repository:write
tl_dr: Edit existing file using exact string replacement with optimistic locking.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    file_path:
      type: string
      description: Path to file within repository
    old_string:
      type: string
      description: Exact string to replace (must match exactly including whitespace)
    new_string:
      type: string
      description: Replacement string
    content_hash:
      type: string
      description: SHA-256 hash for optimistic locking (from get_file_content or previous edit)
    replace_all:
      type: boolean
      description: 'Replace all occurrences of old_string (default: false - replace first only)'
      default: false
  required:
  - repository_alias
  - file_path
  - old_string
  - new_string
  - content_hash
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the file edit succeeded
    file_path:
      type: string
      description: Relative path to edited file (present when success=true)
    content_hash:
      type: string
      description: SHA-256 hash of updated file content for future edits (present when success=true)
    modified_at:
      type: string
      description: ISO 8601 timestamp when file was modified (present when success=true)
    changes_made:
      type: integer
      description: Number of replacements made (1 if replace_all=false, N if replace_all=true) (present when success=true)
    error:
      type: string
      description: Error message (present when success=false)
  required:
  - success
---

Edit existing file using exact string replacement with optimistic locking. USE CASES: (1) Update source code, (2) Modify configurations, (3) Fix bugs. OPTIMISTIC LOCKING: content_hash prevents concurrent edit conflicts - hash from get_file_content or previous edit. STRING REPLACEMENT: old_string must match exactly (including whitespace). Use replace_all=true to replace all occurrences. REQUIREMENTS: File must exist, content_hash must match current state. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/auth.py", "old_string": "def old_func():", "new_string": "def new_func():", "content_hash": "abc123def456"}