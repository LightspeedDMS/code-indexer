---
name: git_mark_resolved
category: git
required_permission: repository:write
tl_dr: Mark a conflicted file as resolved after editing to remove conflict markers.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    file_path:
      type: string
      description: Path to the resolved file (relative to repository root)
  required:
  - repository_alias
  - file_path
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the file was staged successfully
    file:
      type: string
      description: Path of the resolved file
    remaining_conflicts:
      type: integer
      description: Number of remaining unresolved conflicts
    all_resolved:
      type: boolean
      description: Whether all conflicts are now resolved
    message:
      type: string
      description: Status message
---

TL;DR: Mark a conflicted file as resolved after editing to remove conflict markers. USE CASES: (1) Stage a resolved file during merge conflict resolution, (2) Check remaining conflict count, (3) Know when all conflicts are resolved for commit. REQUIREMENTS: Write mode must be active. File must be in conflicted state. Conflict markers must be removed before marking resolved. PERMISSIONS: Requires repository:write + write_mode. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/app.py"} Returns: {"success": true, "file": "src/app.py", "remaining_conflicts": 1, "all_resolved": false, "message": "1 conflict(s) remaining."}
