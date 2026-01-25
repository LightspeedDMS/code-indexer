---
name: git_file_at_revision
category: git
required_permission: query_repos
tl_dr: View a file's contents as it existed at any commit, branch, or tag.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Repository identifier: either an alias (e.g., ''my-project'' or ''my-project-global'') or full path. Use
        list_global_repos to see available repositories and their aliases.'
    path:
      type: string
      description: 'Path to the file, relative to repository root. Must be exact path to a file (not directory). Example:
        ''src/utils/helper.py''.'
    revision:
      type: string
      description: 'The revision to get the file from. Can be commit SHA (full or abbreviated), branch name, tag, or symbolic
        reference. Examples: ''abc1234'' (commit), ''main'' (branch), ''v1.0.0'' (tag), ''HEAD~5'' (5 commits ago), ''feature/auth''
        (branch).'
  required:
  - repository_alias
  - path
  - revision
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    path:
      type: string
      description: File path requested
    revision:
      type: string
      description: Revision requested
    resolved_revision:
      type: string
      description: Resolved full commit SHA
    content:
      type: string
      description: File content at the revision
    size_bytes:
      type: integer
      description: Size of the file in bytes
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: View a file's contents as it existed at any commit, branch, or tag. WHEN TO USE: (1) See old version of a file, (2) Compare file before/after changes, (3) View file at specific tag. WHEN NOT TO USE: Commits that modified file -> git_file_history | Who wrote each line -> git_blame | Full commit details -> git_show_commit. RELATED TOOLS: git_file_history (commits modifying file), git_blame (line attribution), git_show_commit (commit details).