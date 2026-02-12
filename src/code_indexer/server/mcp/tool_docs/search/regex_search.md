---
name: regex_search
category: search
required_permission: query_repos
tl_dr: Direct pattern search on files without index - comprehensive but slower.
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier(s): String for single repo search, array of strings for omni-regex search across
        multiple repos. Use list_global_repos to see available repositories.'
    aggregation_mode:
      type: string
      enum:
      - global
      - per_repo
      default: global
      description: 'Multi-repo aggregation. ''global'' (default): top N by score across all repos. ''per_repo'': distributes
        N evenly across repos. IMPORTANT: limit=10 with 3 repos returns 10 TOTAL (not 30). per_repo distributes as 4+3+3=10.'
    pattern:
      type: string
      description: 'Regular expression pattern (ripgrep syntax).'
    path:
      type: string
      description: Subdirectory to search (relative to repo root).
    include_patterns:
      type: array
      items:
        type: string
      description: Glob patterns for files to include.
    exclude_patterns:
      type: array
      items:
        type: string
      description: Glob patterns for files to exclude.
    case_sensitive:
      type: boolean
      description: Case-sensitive matching.
      default: true
    context_lines:
      type: integer
      description: Lines of context before/after match.
      default: 0
      minimum: 0
      maximum: 10
    max_results:
      type: integer
      description: Maximum matches to return.
      default: 100
      minimum: 1
      maximum: 1000
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
  - pattern
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether succeeded
    matches:
      type: array
      description: Array of regex match results
      items:
        type: object
        properties:
          file_path:
            type: string
          line_number:
            type: integer
          column:
            type: integer
          line_content:
            type: string
          context_before:
            type: array
            items:
              type: string
          context_after:
            type: array
            items:
              type: string
    total_matches:
      type: integer
    truncated:
      type: boolean
    search_engine:
      type: string
    search_time_ms:
      type: number
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Exhaustive regex pattern search on repository files without using indexes. Slower than search_code but guarantees finding ALL matches.

KEY DIFFERENCE: regex_search searches files directly (comprehensive, slower) vs search_code FTS mode which uses indexes (fast, approximate). Use regex_search when you need guaranteed complete results.

EXAMPLE: regex_search(repository_alias='backend-global', pattern='def authenticate')