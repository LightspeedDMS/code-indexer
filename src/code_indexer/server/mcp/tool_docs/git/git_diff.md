---
name: git_diff
category: git
required_permission: query_repos
tl_dr: Show line-by-line changes between two revisions (commits, branches, tags).
---

TL;DR: Show line-by-line changes between two revisions (commits, branches, tags). WHEN TO USE: (1) Compare two commits/branches, (2) See what changed between releases, (3) Review branch differences. WHEN NOT TO USE: Find commits where code was added/removed -> git_search_diffs | Single commit's changes -> git_show_commit | Browse history -> git_log. RELATED TOOLS: git_show_commit (single commit diff), git_search_diffs (find code changes), git_log (find commits).