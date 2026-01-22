---
name: git_branch_delete
category: git
required_permission: repository:admin
tl_dr: Delete a git branch (DESTRUCTIVE).
---

TL;DR: Delete a git branch (DESTRUCTIVE). USE CASES: (1) Delete merged feature branch, (2) Remove obsolete branch, (3) Clean up branches. SAFETY: Requires confirmation_token to prevent accidental deletion. Cannot delete current branch. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: {"repository_alias": "my-repo", "branch_name": "old-feature", "confirmation_token": "CONFIRM_DELETE_BRANCH"} Returns: {"success": true, "deleted_branch": "old-feature"}