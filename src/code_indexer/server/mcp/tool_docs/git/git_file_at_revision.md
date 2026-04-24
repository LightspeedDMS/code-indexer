---
name: git_file_at_revision
category: git
required_permission: query_repos
tl_dr: View a file's contents as it existed at any commit, branch, or tag.
---

TL;DR: View a file's contents as it existed at any commit, branch, or tag. WHEN TO USE: (1) See old version of a file, (2) Compare file before/after changes, (3) View file at specific tag. WHEN NOT TO USE: Commits that modified file -> git_file_history | Who wrote each line -> git_blame | Full commit details -> git_show_commit. RELATED TOOLS: git_file_history (commits modifying file), git_blame (line attribution), git_show_commit (commit details).