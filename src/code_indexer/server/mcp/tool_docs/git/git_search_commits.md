---
name: git_search_commits
category: git
required_permission: query_repos
tl_dr: Search commit messages for keywords, ticket numbers, or patterns.
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier: either an alias (e.g., ''my-project'') or full path (e.g., ''/home/user/repos/my-project'').
        Use list_global_repos to see available repositories and their aliases.'
    query:
      type: string
      description: 'Text or pattern to search for in commit messages. Case-insensitive by default. Examples: ''fix authentication'',
        ''JIRA-123'', ''refactor.*database''. Use is_regex=true for regex patterns.'
    is_regex:
      type: boolean
      description: 'Treat query as a regular expression. Default: false (literal text search). When true, uses POSIX extended
        regex syntax. Example patterns: ''JIRA-\d+'' for ticket numbers, ''fix(ed)?\s+bug'' for variations.'
      default: false
    author:
      type: string
      description: 'Filter to commits by this author. Matches name or email, partial match supported. Default: all authors.
        Examples: ''john@example.com'', ''John''.'
    since:
      type: string
      description: 'Search only commits after this date. Format: YYYY-MM-DD or relative like ''6 months ago''. Default: no
        date limit. Useful to focus on recent history.'
    until:
      type: string
      description: 'Search only commits before this date. Format: YYYY-MM-DD or relative. Default: no date limit. Combine
        with since for date ranges.'
    limit:
      type: integer
      description: 'Maximum number of matching commits to return. Default: 50. Range: 1-500. Popular search terms may match
        many commits.'
      default: 50
      minimum: 1
      maximum: 500
    aggregation_mode:
      type: string
      enum:
      - global
      - per_repo
      description: 'How to aggregate commit search results across multiple repositories. ''global'' (default): Returns top
        N commits by relevance across ALL repos - best for finding most relevant matches. ''per_repo'': Distributes N results
        evenly across repos - ensures balanced representation (e.g., limit=30 across 3 repos returns ~10 commits from each
        repo).'
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
  - query
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    query:
      type: string
      description: Search query used
    is_regex:
      type: boolean
      description: Whether regex mode was used
    matches:
      type: array
      description: List of matching commits
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
          subject:
            type: string
            description: Commit subject line
          body:
            type: string
            description: Full commit message body
          match_highlights:
            type: array
            items:
              type: string
            description: Lines containing matches
    total_matches:
      type: integer
      description: Number of matching commits
    truncated:
      type: boolean
      description: Whether results were truncated
    search_time_ms:
      type: number
      description: Search execution time in ms
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Search commit messages for keywords, ticket numbers, or patterns. WHEN TO USE: (1) Find commits mentioning 'JIRA-123', (2) Search for 'fix bug', (3) Find feature-related commits by message. WHEN NOT TO USE: Find when code was added/removed -> git_search_diffs | Browse recent history -> git_log | Commit details -> git_show_commit. RELATED TOOLS: git_search_diffs (search code changes), git_show_commit (view commit), git_log (browse history).