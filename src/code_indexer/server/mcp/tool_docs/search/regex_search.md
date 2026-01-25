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
      description: 'How to aggregate regex search results across multiple repositories. ''global'' (default): Returns top
        N matches by relevance across ALL repos - best for finding absolute best matches (e.g., limit=20 across 3 repos returns
        20 best total). ''per_repo'': Distributes N results evenly across repos - ensures balanced representation (e.g., limit=20
        across 3 repos returns ~7 from each repo).'
    pattern:
      type: string
      description: 'Regular expression pattern to search for. Uses ripgrep regex syntax. Examples: ''def\s+test_'' matches
        Python test functions, ''TODO|FIXME'' matches either word.'
    path:
      type: string
      description: 'Subdirectory to search within (relative to repo root). Default: search entire repository.'
    include_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to include. Examples: [''*.py''] for Python files, [''*.ts'', ''*.tsx''] for TypeScript.'
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to exclude. Examples: [''*_test.py''] to exclude tests, [''node_modules/**'']
        to exclude deps.'
    case_sensitive:
      type: boolean
      description: 'Whether search is case-sensitive. Default: true.'
      default: true
    context_lines:
      type: integer
      description: 'Lines of context before/after match. Default: 0.'
      default: 0
      minimum: 0
      maximum: 10
    max_results:
      type: integer
      description: 'Maximum matches to return. Default: 100.'
      default: 100
      minimum: 1
      maximum: 1000
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

TL;DR: Direct pattern search on files without index - comprehensive but slower. WHEN TO USE: (1) Find exact text/identifiers: 'def authenticate_user', (2) Complex patterns: 'class.*Controller', (3) TODO/FIXME comments, (4) Comprehensive search when you need ALL matches (not approximate). WHEN NOT TO USE: (1) Conceptual queries like 'authentication logic' -> use search_code(semantic), (2) Fast repeated searches -> use search_code(fts) which is indexed. COMPARISON: regex_search = comprehensive/slower (searches files directly) | search_code(fts) = fast/indexed (may miss unindexed files) | search_code(semantic) = conceptual/approximate (finds by meaning, not text). RELATED TOOLS: search_code (pre-indexed semantic/FTS search), git_search_diffs (find code changes in git history). QUICK START: regex_search('backend-global', 'def authenticate') finds all function definitions. EXAMPLE: regex_search('backend-global', 'TODO|FIXME', include_patterns=['*.py'], context_lines=1) Returns: {"success": true, "matches": [{"file_path": "src/auth.py", "line": 42, "content": "# TODO: add input validation", "context_before": ["def login(user):"], "context_after": ["    pass"]}], "total_matches": 3}