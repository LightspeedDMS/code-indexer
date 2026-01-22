---
name: git_reset
category: git
required_permission: repository:admin
tl_dr: Reset working tree to specific state (DESTRUCTIVE).
---

TL;DR: Reset working tree to specific state (DESTRUCTIVE). Reset working tree to specific state (DESTRUCTIVE). USE CASES: (1) Discard commits, (2) Reset to specific commit, (3) Clean working tree. MODES: soft (keep changes staged), mixed (keep changes unstaged), hard (discard all changes). SAFETY: Requires explicit mode. Optional commit_hash and confirmation_token for destructive operations. PERMISSIONS: Requires repository:admin (destructive operation). EXAMPLE: {"repository_alias": "my-repo", "mode": "hard", "commit_hash": "abc123", "confirmation_token": "CONFIRM_RESET"} Returns: {"success": true, "reset_mode": "hard", "target_commit": "abc123"}