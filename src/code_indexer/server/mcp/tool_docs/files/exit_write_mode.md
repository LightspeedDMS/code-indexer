---
name: exit_write_mode
category: files
required_permission: repository:write
tl_dr: Exit write mode for a write-exception repository, triggering a synchronous refresh.
slim_description: "Leave write mode on a write-exception repository such as cidx-meta-global: releases the write lock, then refreshes the repository synchronously so the queryable snapshot includes your changes."
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

Exit write mode for a write-exception repository such as cidx-meta-global.

WHAT IT DOES, in order: (1) removes the write-mode marker, so reads go back to the versioned snapshot, (2) releases the exclusive write lock, (3) stops the auto-watch on the source directory, (4) runs a refresh of the repository synchronously.

BLOCKS UNTIL COMPLETE: The tool does not return until the refresh finishes, so the queryable snapshot reflects your changes when it returns: `{"success": true, "message": "Refresh complete, write mode exited for '<alias>'"}`.

NON-WRITE-EXCEPTION REPOS: Returns `{"success": true, "message": "no-op: '<alias>' is not a write-exception repo"}`; nothing is refreshed.

NOT IN WRITE MODE: Returns `{"success": true, "warning": "Write mode was not active for '<alias>'", "message": "not in write mode — nothing to exit"}`.

ERRORS: `{"success": false, "error": "..."}`, for example `Missing required parameter: repo_alias`, `RefreshScheduler not available`, or a refresh failure.

WRITE MODE WORKFLOW: call enter_write_mode -> use create_file/edit_file/delete_file -> call exit_write_mode.

ALWAYS CALL EXIT: Until exit_write_mode runs (or the lock expires), the write lock stays held and scheduled refreshes of the repository wait for it, and your edits are not refreshed into the queryable snapshot.

PERMISSIONS: Requires repository:write.

EXAMPLE: {"repo_alias": "cidx-meta-global"}
