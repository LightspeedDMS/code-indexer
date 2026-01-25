---
name: git_show_commit
category: git
required_permission: query_repos
tl_dr: View detailed info about a single commit (message, stats, diff).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Repository identifier: either an alias (e.g., ''my-project'' or ''my-project-global'') or full path. Use
        list_global_repos to see available repositories and their aliases.'
    commit_hash:
      type: string
      description: 'The commit to show. Can be full SHA (40 chars), abbreviated SHA (7+ chars), or symbolic reference like
        ''HEAD'', ''HEAD~3'', ''main^''. Examples: ''abc1234'', ''abc1234def5678...'', ''HEAD~1''.'
    include_diff:
      type: boolean
      description: 'Whether to include the full diff in the response. Default: false. Set to true to see exactly what lines
        changed. Warning: large commits may produce very long diffs.'
      default: false
    include_stats:
      type: boolean
      description: 'Whether to include file change statistics (files changed, insertions, deletions). Default: true. Provides
        quick summary of commit scope.'
      default: true
  required:
  - repository_alias
  - commit_hash
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    commit:
      type: object
      description: Commit metadata
      properties:
        hash:
          type: string
        short_hash:
          type: string
        author_name:
          type: string
        author_email:
          type: string
        author_date:
          type: string
        committer_name:
          type: string
        committer_email:
          type: string
        committer_date:
          type: string
        subject:
          type: string
        body:
          type: string
    stats:
      type:
      - array
      - 'null'
      description: File change statistics (when include_stats=true)
      items:
        type: object
        properties:
          path:
            type: string
          insertions:
            type: integer
          deletions:
            type: integer
          status:
            type: string
            enum:
            - added
            - modified
            - deleted
            - renamed
    diff:
      type:
      - string
      - 'null'
      description: Full diff (when include_diff=true)
    parents:
      type: array
      items:
        type: string
      description: Parent commit SHAs
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: View detailed info about a single commit (message, stats, diff). WHEN TO USE: (1) Examine a specific commit, (2) See what files changed, (3) Get full diff of one commit. WHEN NOT TO USE: Browse commit history -> git_log | Compare two different revisions -> git_diff | View file at commit -> git_file_at_revision. RELATED TOOLS: git_log (find commits), git_diff (compare revisions), git_file_at_revision (view file content).