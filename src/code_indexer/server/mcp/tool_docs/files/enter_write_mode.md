---
name: enter_write_mode
category: files
required_permission: repository:write
tl_dr: Enter write mode for a write-exception repository (e.g. cidx-meta-global).
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
      description: Informational message (present for no-op results)
    warning:
      type: string
      description: Warning message (present when write mode could not be entered)
    error:
      type: string
      description: Error message (present when success=false)
  required:
  - success
---

Enter write mode for a write-exception repository such as cidx-meta-global. WHAT IT DOES: (1) Acquires an exclusive write lock on the repository, (2) Creates a marker file that redirects reads to the live source directory, (3) Returns the source_path for the caller's reference. WRITE MODE WORKFLOW: call enter_write_mode -> use create_file/edit_file/delete_file -> call exit_write_mode. EXIT IS MANDATORY: Always call exit_write_mode when done — it triggers a synchronous refresh so the versioned snapshot reflects your changes. NON-WRITE-EXCEPTION REPOS: Returns success with a no-op message; no lock is acquired. LOCK FAILURE: Returns success=false if the lock is already held by another process. PERMISSIONS: Requires repository:write. EXAMPLE: {"repo_alias": "cidx-meta-global"}
