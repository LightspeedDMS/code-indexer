---
name: git_blame
category: git
required_permission: query_repos
tl_dr: See who wrote each line of a file and when (line-by-line attribution).
---

TL;DR: See who wrote each line of a file and when (line-by-line attribution). WHEN TO USE: (1) 'Who wrote this code?', (2) Find who introduced a bug, (3) Understand code ownership. WHEN NOT TO USE: File's commit history -> git_file_history | Full commit details -> git_show_commit. RELATED TOOLS: git_file_history (commits that modified file), git_show_commit (commit details), git_file_at_revision (view file at any commit).