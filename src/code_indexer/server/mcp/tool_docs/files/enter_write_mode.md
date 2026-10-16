---
name: enter_write_mode
category: files
required_permission: repository:write
tl_dr: Enter write mode for a write-exception repository (e.g. cidx-meta-global).
slim_description: "Enter write mode on a write-exception repository such as cidx-meta-global: acquires its exclusive write lock and points reads at the live source directory until exit_write_mode is called."
inputSchema:
  type: object
  properties:
    repo_alias:
      type: string
      description: Repository alias that supports write mode (e.g. 'cidx-meta-global')
  required:
  - repo_alias
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether write mode was entered (or was a no-op for non-write-exception repos)
    alias:
      type: string
      description: Repository alias (present when write mode was entered)
    source_path:
      type: string
      description: Absolute path to the live source directory being edited (present when write mode was entered)
    message:
      type: string
      description: Informational message (present for no-op results, and when the write lock is already held)
    warning:
      type: string
      description: Not returned by this tool
    error:
      type: string
      description: Error message (present when success=false)
  required:
  - success
---

Enter write mode for a write-exception repository such as cidx-meta-global.

WHAT IT DOES: (1) Acquires the repository's exclusive write lock, (2) writes a write-mode marker that points reads of the repository at its live source directory, (3) returns `{"success": true, "alias": "<repo_alias>", "source_path": "<live source directory>"}`.

WRITE MODE WORKFLOW: call enter_write_mode -> use create_file/edit_file/delete_file -> call exit_write_mode.

EXIT IS MANDATORY: Always call exit_write_mode when done. It releases the lock and runs a synchronous refresh so the versioned snapshot reflects your changes.

NON-WRITE-EXCEPTION REPOS: Returns `{"success": true, "message": "no-op: '<alias>' is not a write-exception repo"}`; no lock is acquired.

LOCK ALREADY HELD: Returns `{"success": false, "message": "Write lock for '<alias>' is already held by '<owner>'"}`.

ERRORS: `{"success": false, "error": "..."}`, for example `Missing required parameter: repo_alias` or `RefreshScheduler not available`.

PERMISSIONS: Requires repository:write.

EXAMPLE: {"repo_alias": "cidx-meta-global"}
