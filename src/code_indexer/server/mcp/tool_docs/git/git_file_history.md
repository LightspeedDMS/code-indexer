---
name: git_file_history
category: git
required_permission: query_repos
tl_dr: Get all commits that modified a specific file.
---

TL;DR: Get all commits that modified a specific file. WHEN TO USE: (1) Track file evolution, (2) Find when bug was introduced, (3) See who worked on a file. WHEN NOT TO USE: Repo-wide history -> git_log | Line attribution -> git_blame | View old version -> git_file_at_revision. RELATED TOOLS: git_log (repo-wide history, can also filter by path), git_blame (who wrote each line), git_file_at_revision (view file at commit).