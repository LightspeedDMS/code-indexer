---
name: git_file_history
category: git
required_permission: query_repos
tl_dr: Get all commits that modified a specific file.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias or full path.
    path:
      type: string
      description: 'Path to file (relative to repo root). Must be a file path, not directory. Examples: ''src/auth/login.py'',
        ''package.json'', ''docs/API.md''.'
    limit:
      type: integer
      description: 'Maximum commits to return. Default: 50. Range: 1-500. For files with long history, start with lower limits
        and use date filters to narrow results.'
      default: 50
      minimum: 1
      maximum: 500
    follow_renames:
      type: boolean
      description: 'Follow file history across renames. Default: true.'
      default: true
  required:
  - repository_alias
  - path
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    path:
      type: string
    commits:
      type: array
    total_count:
      type: integer
    truncated:
      type: boolean
    renamed_from:
      type:
      - string
      - 'null'
    error:
      type: string
  required:
  - success
---

TL;DR: Get all commits that modified a specific file. WHEN TO USE: (1) Track file evolution, (2) Find when bug was introduced, (3) See who worked on a file. WHEN NOT TO USE: Repo-wide history -> git_log | Line attribution -> git_blame | View old version -> git_file_at_revision. RELATED TOOLS: git_log (repo-wide history, can also filter by path), git_blame (who wrote each line), git_file_at_revision (view file at commit).