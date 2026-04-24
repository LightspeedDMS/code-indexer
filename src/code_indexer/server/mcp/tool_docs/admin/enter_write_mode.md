---
name: enter_write_mode
category: admin
required_permission: repository:write
tl_dr: Enter write mode for a write-exception repository such as cidx-meta-global.
---

Enter write mode for a write-exception repository such as cidx-meta-global. WHAT IT DOES: (1) Acquires an exclusive write lock on the repository, (2) Creates a marker file that redirects reads to the live source directory, (3) Returns the source_path for the caller's reference. WRITE MODE WORKFLOW: call enter_write_mode -> use create_file/edit_file/delete_file -> call exit_write_mode. EXIT IS MANDATORY: Always call exit_write_mode when done — it triggers a synchronous refresh so the versioned snapshot reflects your changes. NON-WRITE-EXCEPTION REPOS: Returns success with a no-op message; no lock is acquired. LOCK FAILURE: Returns success=false if the lock is already held by another process. PERMISSIONS: Requires repository:write. EXAMPLE: {"repo_alias": "cidx-meta-global"}
