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
      description: 'Repository identifier: either an alias (e.g., ''my-project'' or ''my-project-global'') or full path. Use
        list_global_repos to see available repositories and their aliases.'
    limit:
      type: integer
      description: 'Maximum number of commits to return. Default: 50. Range: 1-500. Lower values for quick overview, higher
        for comprehensive history.'
      default: 50
      minimum: 1
      maximum: 500
    offset:
      type: integer
      description: 'Number of commits to skip (for pagination). Default: 0. Use with limit to paginate through history. Example:
        offset=50, limit=50 returns commits 51-100.'
      default: 0
      minimum: 0
    path:
      type: string
      description: 'Filter commits to only those affecting this path (file or directory). Path is relative to repo root. Examples:
        ''src/main.py'' for single file, ''src/'' for all files under src directory.'
    author:
      type: string
      description: 'Filter commits by author. Matches against author name or email. Partial matches supported. Examples: ''john@example.com'',
        ''John Smith'', ''john''.'
    since:
      type: string
      description: 'Include only commits after this date. Format: YYYY-MM-DD or relative like ''2 weeks ago'', ''2024-01-01''.
        Inclusive of the date.'
    until:
      type: string
      description: 'Include only commits before this date. Format: YYYY-MM-DD or relative like ''yesterday'', ''2024-06-30''.
        Inclusive of the date.'
    branch:
      type: string
      description: 'Branch to get log from. Default: current HEAD. Examples: ''main'', ''feature/auth'', ''origin/develop''.
        Can also be a tag like ''v1.0.0''.'
    aggregation_mode:
      type: string
      enum:
      - global
      - per_repo
      description: 'How to aggregate git log results across multiple repositories. ''global'' (default): Merges commits by
        date across ALL repos - shows complete chronological history. ''per_repo'': Distributes limit evenly across repos
        - ensures balanced representation (e.g., limit=30 across 3 repos returns ~10 commits from each repo).'
      default: global
    response_format:
      type: string
      enum:
      - flat
      - grouped
      default: flat
      description: 'Response format for omni-search (multi-repo) results. Only applies when repository_alias is an array.


        ''flat'' (default): Returns all results in a single array, each with source_repo field.

        Example response: {"results": [{"file_path": "src/auth.py", "source_repo": "backend-global", "content": "...", "score":
        0.95}, {"file_path": "Login.tsx", "source_repo": "frontend-global", "content": "...", "score": 0.89}], "total_results":
        2}


        ''grouped'': Groups results by repository under results_by_repo object.

        Example response: {"results_by_repo": {"backend-global": {"count": 1, "results": [{"file_path": "src/auth.py", "content":
        "...", "score": 0.95}]}, "frontend-global": {"count": 1, "results": [{"file_path": "Login.tsx", "content": "...",
        "score": 0.89}]}}, "total_results": 2}


        Use ''grouped'' when you need to process results per-repository or display results organized by source.'
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