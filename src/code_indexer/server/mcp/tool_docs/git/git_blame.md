---
name: git_blame
category: git
required_permission: query_repos
tl_dr: See who wrote each line of a file and when (line-by-line attribution).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias or full path.
    path:
      type: string
      description: 'Path to file to blame (relative to repo root). Must be a file, not directory. Examples: ''src/auth/login.py'',
        ''lib/utils.js'', ''README.md''.'
    revision:
      type: string
      description: 'Blame file as of this revision. Default: HEAD (current state). Use to see blame at a historical point,
        e.g., before a refactor. Accepts: commit SHA, branch name, tag, or relative ref like ''HEAD~5'' or ''v1.0.0''.'
    start_line:
      type: integer
      description: 'First line to include (1-indexed). Use with end_line to focus on specific code sections in large files.
        Example: start_line=100, end_line=150 blames lines 100 through 150.'
      minimum: 1
    end_line:
      type: integer
      description: Last line to include (1-indexed, inclusive). Must be >= start_line. Omit both start_line and end_line to
        blame entire file.
      minimum: 1
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
    revision:
      type: string
    lines:
      type: array
    unique_commits:
      type: integer
    error:
      type: string
  required:
  - success
---

TL;DR: See who wrote each line of a file and when (line-by-line attribution). WHEN TO USE: (1) 'Who wrote this code?', (2) Find who introduced a bug, (3) Understand code ownership. WHEN NOT TO USE: File's commit history -> git_file_history | Full commit details -> git_show_commit. RELATED TOOLS: git_file_history (commits that modified file), git_show_commit (commit details), git_file_at_revision (view file at any commit).