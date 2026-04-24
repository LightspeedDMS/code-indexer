---
name: git_clean
category: git
required_permission: repository:admin
tl_dr: Remove untracked files from working tree (DESTRUCTIVE).
---

Remove untracked files from working tree (DESTRUCTIVE). USE CASES: (1) Remove build artifacts, (2) Clean untracked files, (3) Restore clean state. SAFETY: Requires confirmation_token to prevent accidental deletion. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: {"repository_alias": "my-repo", "confirmation_token": "CONFIRM_DELETE_UNTRACKED"}