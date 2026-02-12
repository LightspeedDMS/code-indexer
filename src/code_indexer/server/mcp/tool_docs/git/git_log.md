---
name: git_log
category: git
required_permission: query_repos
tl_dr: Browse commit history with filtering by path, author, date, or branch.
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: Repository alias or full path.
    limit:
      type: integer
      description: Maximum commits to return.
      default: 50
      minimum: 1
      maximum: 500
    offset:
      type: integer
      description: Commits to skip for pagination.
      default: 0
      minimum: 0
    path:
      type: string
      description: Filter commits affecting this path (file or directory, relative to repo root).
    author:
      type: string
      description: Filter by author name or email. Partial matches supported.
    since:
      type: string
      description: 'Commits after this date. Format: YYYY-MM-DD or relative (e.g., ''2 weeks ago'').'
    until:
      type: string
      description: 'Commits before this date. Format: YYYY-MM-DD or relative (e.g., ''yesterday'').'
    branch:
      type: string
      description: 'Branch or tag to get log from. Default: current HEAD.'
    aggregation_mode:
      type: string
      enum:
      - global
      - per_repo
      description: 'Multi-repo aggregation. ''global'' (default): top N by score across all repos. ''per_repo'': distributes
        N evenly across repos. IMPORTANT: limit=10 with 3 repos returns 10 TOTAL (not 30). per_repo distributes as 4+3+3=10.'
      default: global
    response_format:
      type: string
      enum:
      - flat
      - grouped
      default: flat
      description: 'Multi-repo result format. ''flat'' (default): single array with source_repo field per result. ''grouped'':
        results organized under results_by_repo by repository.'
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    commits:
      type: array
      description: List of commits matching filters
      items:
        type: object
        properties:
          hash:
            type: string
            description: Full 40-char commit SHA
          short_hash:
            type: string
            description: Abbreviated SHA
          author_name:
            type: string
            description: Author name
          author_email:
            type: string
            description: Author email
          author_date:
            type: string
            description: Author date (ISO 8601)
          committer_name:
            type: string
            description: Committer name
          committer_email:
            type: string
            description: Committer email
          committer_date:
            type: string
            description: Committer date (ISO 8601)
          subject:
            type: string
            description: Commit subject line
          body:
            type: string
            description: Full commit message body
    total_count:
      type: integer
      description: Number of commits returned
    truncated:
      type: boolean
      description: Whether results were truncated
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Browse commit history with filtering by path, author, date, or branch. WHEN TO USE: (1) View recent commits, (2) Find when changes were made, (3) Filter history by author/date/path. WHEN NOT TO USE: Search commit messages for keywords -> git_search_commits | Find when code was added/removed -> git_search_diffs | Single commit details -> git_show_commit. RELATED TOOLS: git_show_commit (commit details), git_search_commits (search messages), git_diff (compare revisions).