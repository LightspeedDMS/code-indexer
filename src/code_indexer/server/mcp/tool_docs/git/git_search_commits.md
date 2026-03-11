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
      description: Repository alias or full path.
    query:
      type: string
      description: Text or pattern to search in commit messages. Case-insensitive by default.
    is_regex:
      type: boolean
      description: Treat query as regular expression (POSIX extended syntax).
      default: false
    author:
      type: string
      description: Filter by author name or email. Partial matches supported.
    since:
      type: string
      description: 'Commits after this date. Format: YYYY-MM-DD or relative (e.g., ''6 months ago'').'
    until:
      type: string
      description: 'Commits before this date. Format: YYYY-MM-DD or relative.'
    limit:
      type: integer
      description: Maximum matching commits to return.
      default: 50
      minimum: 1
      maximum: 500
    response_format:
      type: string
      description: 'Response format for multi-repo queries: flat (default) or grouped by repository'
      enum:
        - flat
        - grouped
      default: flat
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