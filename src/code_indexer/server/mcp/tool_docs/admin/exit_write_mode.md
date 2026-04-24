---
name: exit_write_mode
category: admin
required_permission: repository:write
tl_dr: Exit write mode for a write-exception repository such as cidx-meta-global.
---

Exit write mode for a write-exception repository such as cidx-meta-global. WHAT IT DOES: (1) Calls _execute_refresh() synchronously (blocks until complete), (2) Removes the write-mode marker file, (3) Releases the exclusive write lock. BLOCKS UNTIL COMPLETE: The handler does not return until the refresh finishes, ensuring the versioned snapshot is up to date when this tool returns. NON-WRITE-EXCEPTION REPOS: Returns success with a no-op message; no refresh is triggered. NOT IN WRITE MODE: Returns success with a warning if write mode was not active. WRITE MODE WORKFLOW: call enter_write_mode -> use create_file/edit_file/delete_file -> call exit_write_mode. ALWAYS CALL EXIT: Failing to call exit_write_mode leaves the write lock held, blocking the background refresh scheduler. PERMISSIONS: Requires repository:write. EXAMPLE: {"repo_alias": "cidx-meta-global"}
