---
name: git_conflict_status
category: git
required_permission: repository:read
tl_dr: Get detailed merge conflict status with conflict marker regions per file.
---

TL;DR: Get detailed merge conflict status with conflict marker regions per file. USE CASES: (1) View conflicted files after a merge, (2) See ours/theirs content for each conflict region, (3) Check remaining conflicts during resolution. REQUIREMENTS: Merge must be in progress. PERMISSIONS: Requires repository:read (no write_mode needed). EXAMPLE: {"repository_alias": "my-repo"} Returns: {"in_merge": true, "total_conflicts": 2, "conflicted_files": [{"file": "src/app.py", "status": "UU", "regions": [{"start_line": 10, "end_line": 16, "ours_label": "HEAD", "theirs_label": "feature", "ours_content": "...", "theirs_content": "..."}], "is_binary": false}]}
