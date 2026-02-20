---
name: exit_write_mode
category: files
required_permission: repository:write
tl_dr: Exit write mode for a write-exception repository, triggering a synchronous refresh.
inputSchema:
  type: object
  properties:
    repo_alias:
      type: string
      description: Repository alias to exit write mode for (e.g. 'cidx-meta-global')
  required:
  - repo_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether write mode was exited successfully
    message:
      type: string
      description: Informational message describing the outcome
    warning:
      type: string
      description: Warning message when write mode was not active
    error:
      type: string
      description: Error message (present when success=false)
  required:
  - success
---

Exit write mode for a write-exception repository such as cidx-meta-global. WHAT IT DOES: (1) Calls _execute_refresh() synchronously (blocks until complete), (2) Removes the write-mode marker file, (3) Releases the exclusive write lock. BLOCKS UNTIL COMPLETE: The handler does not return until the refresh finishes, ensuring the versioned snapshot is up to date when this tool returns. NON-WRITE-EXCEPTION REPOS: Returns success with a no-op message; no refresh is triggered. NOT IN WRITE MODE: Returns success with a warning if write mode was not active. WRITE MODE WORKFLOW: call enter_write_mode -> use create_file/edit_file/delete_file -> call exit_write_mode. ALWAYS CALL EXIT: Failing to call exit_write_mode leaves the write lock held, blocking the background refresh scheduler. PERMISSIONS: Requires repository:write. EXAMPLE: {"repo_alias": "cidx-meta-global"}
