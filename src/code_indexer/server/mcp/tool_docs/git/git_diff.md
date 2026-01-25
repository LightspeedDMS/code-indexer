---
name: git_diff
category: git
required_permission: query_repos
tl_dr: Show line-by-line changes between two revisions (commits, branches, tags).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias or full path.
    from_revision:
      type: string
      description: 'Starting revision (the ''before'' state). Accepts: commit SHA (full or abbreviated), branch name (''main'',
        ''develop''), tag (''v1.0.0''), relative refs (''HEAD~3'', ''main~5''), or ''HEAD''. Example: ''abc123'' or ''main~2''.'
    to_revision:
      type: string
      description: 'Ending revision (the ''after'' state). Default: HEAD. Same formats as from_revision. Common patterns:
        ''HEAD'' (latest), branch name, commit SHA. Example: Compare feature branch to main with from=''main'', to=''feature-x''.'
    path:
      type: string
      description: 'Limit diff to this path (file or directory). Relative to repo root. Use to focus on specific files/directories
        in large diffs. Examples: ''src/auth.py'', ''lib/utils/'', ''*.md'' (all markdown files).'
    context_lines:
      type: integer
      description: 'Context lines around changes. Default: 3.'
      default: 3
      minimum: 0
      maximum: 20
    stat_only:
      type: boolean
      description: 'Return only statistics without hunks. Default: false.'
      default: false
    offset:
      type: integer
      description: 'Number of diff lines to skip (for pagination). Default: 0. Use with limit to paginate through large diffs.'
      default: 0
      minimum: 0
    limit:
      type: integer
      description: 'Maximum number of diff lines to return. Default: 500. Range: 1-5000. Use for paginating large diffs. Response
        includes has_more and next_offset.'
      default: 500
      minimum: 1
      maximum: 5000
  required:
  - repository_alias
  - from_revision
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    from_revision:
      type: string
    to_revision:
      type:
      - string
      - 'null'
    files:
      type: array
    total_insertions:
      type: integer
    total_deletions:
      type: integer
    stat_summary:
      type: string
    error:
      type: string
    lines_returned:
      type: integer
    total_lines:
      type: integer
    has_more:
      type: boolean
    next_offset:
      type:
      - integer
      - 'null'
  required:
  - success
---

TL;DR: Show line-by-line changes between two revisions (commits, branches, tags). WHEN TO USE: (1) Compare two commits/branches, (2) See what changed between releases, (3) Review branch differences. WHEN NOT TO USE: Find commits where code was added/removed -> git_search_diffs | Single commit's changes -> git_show_commit | Browse history -> git_log. RELATED TOOLS: git_show_commit (single commit diff), git_search_diffs (find code changes), git_log (find commits).