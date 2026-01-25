---
name: git_search_diffs
category: git
required_permission: query_repos
tl_dr: Find when specific code was added/removed in git history (pickaxe search).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Repository identifier: either an alias (e.g., ''my-project'') or full path (e.g., ''/home/user/repos/my-project'').
        Use list_global_repos to see available repositories and their aliases.'
    search_string:
      type: string
      description: 'Exact string to search for in diff content. Finds commits where this string was added or removed. Use
        for function names, variable names, or specific code. Example: ''calculateTotalPrice''. Mutually exclusive with search_pattern.'
    search_pattern:
      type: string
      description: 'Regex pattern to search for in diff content. Finds commits where lines matching the pattern were added
        or removed. Use for flexible matching. Example: ''def\s+calculate.*'' to find function definitions. Mutually exclusive
        with search_string. Requires is_regex=true.'
    is_regex:
      type: boolean
      description: 'When true, use search_pattern as regex (-G flag). When false, use search_string as literal (-S flag).
        Default: false. Regex is slower but more flexible.'
      default: false
    path:
      type: string
      description: 'Limit search to diffs in this path (file or directory). Relative to repo root. Default: entire repository.
        Examples: ''src/auth/'', ''lib/utils.py''.'
    since:
      type: string
      description: 'Search only commits after this date. Format: YYYY-MM-DD or relative. Default: no limit. Useful to narrow
        down large search results.'
    until:
      type: string
      description: 'Search only commits before this date. Format: YYYY-MM-DD or relative. Default: no limit.'
    limit:
      type: integer
      description: 'Maximum number of matching commits to return. Default: 50. Range: 1-200. Diff search is computationally
        expensive; lower limits recommended. Response indicates if results were truncated.'
      default: 50
      minimum: 1
      maximum: 200
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    search_term:
      type: string
      description: Search term used
    is_regex:
      type: boolean
      description: Whether regex mode was used
    matches:
      type: array
      description: List of commits that added/removed matching content
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
          author_date:
            type: string
            description: Author date (ISO 8601)
          subject:
            type: string
            description: Commit subject line
          files_changed:
            type: array
            items:
              type: string
            description: Files modified in this commit
          diff_snippet:
            type:
            - string
            - 'null'
            description: Relevant portion of diff (if available)
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

TL;DR: Find when specific code was added/removed in git history (pickaxe search). WHAT IS PICKAXE? Git's term for searching code CHANGES (not commit messages). Finds commits where text was introduced or deleted. WHEN TO USE: (1) 'When was this function added?', (2) 'Who introduced this bug?', (3) Track code pattern evolution. WHEN NOT TO USE: Search commit messages -> use git_search_commits instead. WARNING: Can be slow on large repos (may take 1-3+ minutes). Start with limit=5. RELATED TOOLS: git_search_commits (searches commit messages), git_blame (who wrote current code), git_show_commit (view commit details).