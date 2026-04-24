---
name: git_merge
category: git
required_permission: repository:write
tl_dr: Merge a source branch into the current branch with detailed conflict detection.
---

TL;DR: Merge a source branch into the current branch with detailed conflict detection. USE CASES: (1) Integrate upstream changes, (2) Merge feature branches, (3) Detect and list merge conflicts. REQUIREMENTS: Write mode must be active (use enter_write_mode first). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "source_branch": "feature/login"} Returns on success: {"success": true, "merge_summary": "..."} Returns on conflict: {"success": false, "conflicts": [{"file": "src/app.py", "status": "UU", "conflict_type": "content", "is_binary": false}]} After conflicts, use git_merge_abort to roll back to pre-merge state.
