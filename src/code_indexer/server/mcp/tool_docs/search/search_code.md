---
name: search_code
category: search
required_permission: query_repos
tl_dr: Search code using pre-built indexes.
inputSchema:
  type: object
  properties:
    query_text:
      type: string
      description: 'Search query text. MULTI-TERM FTS QUERIES: When using search_mode=''fts'' with multiple terms (e.g., ''authenticate
        user''), ALL terms must match (AND semantics). Single-term queries match normally. For OR semantics, use separate
        queries or regex mode with ''|'' operator (e.g., ''term1|term2'').'
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository alias(es) to search. FORMATS: (1) String for single repo: ''backend-global'', (2) Array for
        multi-repo: [''backend-global'', ''frontend-global''], (3) Wildcard pattern: ''*-global'' (all global repos) or ''pch-*-global''
        (pattern match). Multi-repo searches support aggregation_mode and response_format parameters for result organization.'
    aggregation_mode:
      type: string
      enum:
      - global
      - per_repo
      default: global
      description: 'Result aggregation for multi-repo searches. ''global'' (default): Top N results by score across ALL repos
        - best for finding absolute best matches anywhere. ''per_repo'': Distributes N results evenly across repos - best
        for comparing implementations or ensuring representation from each repo. LIMIT MATH: limit=10 with 3 repos in ''global''
        mode might return 7+3+0=10 total. In ''per_repo'' mode returns 4+3+3=10 total (NOT 30 - per_repo does NOT multiply
        the limit).'
    exclude_patterns:
      type: array
      items:
        type: string
      description: Regex patterns to exclude repositories from omni-search.
    limit:
      type: integer
      description: 'Maximum number of results. IMPORTANT: Start with limit=5 to conserve context tokens. Each result consumes
        tokens proportional to code snippet size. Only increase limit if initial results insufficient. High limits (>20) can
        rapidly consume context window.'
      default: 10
      minimum: 1
      maximum: 100
    min_score:
      type: number
      description: Minimum similarity score
      default: 0.5
      minimum: 0
      maximum: 1
    search_mode:
      type: string
      description: 'Search mode: ''semantic'' for natural language/conceptual queries (''how authentication works''), ''fts''
        for exact text/identifiers (''def authenticate_user''), ''hybrid'' for both. Default: semantic. NOTE: FTS multi-term
        queries use AND semantics - all terms must match. Example: ''password reset'' requires both words. For OR behavior,
        use regex mode.'
      enum:
      - semantic
      - fts
      - hybrid
      default: semantic
    language:
      type: string
      description: 'Filter by programming language. Supported languages: c, cpp, csharp, dart, go, java, javascript, kotlin,
        php, python, ruby, rust, scala, swift, typescript, css, html, vue, markdown, xml, json, yaml, bash, shell, and more.
        Can use friendly names or file extensions (py, js, ts, etc.).'
    exclude_language:
      type: string
      description: Exclude files of specified language. Use same language names as --language parameter.
    path_filter:
      type: string
      description: Filter by file path pattern using glob syntax (e.g., '*/tests/*' for test files, '*/src/**/*.py' for Python
        files in src). Supports *, **, ?, [seq] wildcards.
    exclude_path:
      type: string
      description: 'Exclude files matching path pattern. Supports glob patterns (*, **, ?, [seq]). COMMON NOISE FILTERS: ''**/package-lock.json''
        (npm), ''**/yarn.lock'' (yarn), ''**/node_modules/**'' (deps), ''**/test/fixtures/**'' (test data), ''**/*.min.js''
        (minified). Can combine with comma: ''pattern1,pattern2'' or use multiple calls.'
    file_extensions:
      type: array
      items:
        type: string
      description: Filter by file extensions (e.g., [".py", ".js"]). Alternative to language filter when you need exact extension
        matching.
    accuracy:
      type: string
      enum:
      - fast
      - balanced
      - high
      default: balanced
      description: 'Search accuracy profile: ''fast'' (lower accuracy, faster response), ''balanced'' (default, good tradeoff),
        ''high'' (higher accuracy, slower response). Affects embedding search precision.'
    time_range:
      type: string
      description: 'Time range filter for temporal queries (format: YYYY-MM-DD..YYYY-MM-DD, e.g., ''2024-01-01..2024-12-31'').
        Returns only code that existed during this period. Requires temporal index built with ''cidx index --index-commits''.
        Check repository''s temporal support via global_repo_status - look for enable_temporal: true in the response. Empty
        temporal query results typically indicate temporal indexing is not enabled for the repository.'
    time_range_all:
      type: boolean
      default: false
      description: Query across all git history without time range limit. Requires temporal index built with 'cidx index --index-commits'.
        Equivalent to querying from first commit to HEAD.
    at_commit:
      type: string
      description: Query code at a specific commit hash or ref (e.g., 'abc123' or 'HEAD~5'). Returns code state as it existed
        at that commit. Requires temporal index.
    include_removed:
      type: boolean
      default: false
      description: Include files that have been removed from the current HEAD in search results. Only applicable with temporal
        queries. Removed files will have is_removed flag in temporal_context.
    show_evolution:
      type: boolean
      default: false
      description: Include code evolution timeline with commit history and diffs in response. Shows how code changed over
        time. Requires temporal index.
    evolution_limit:
      type: integer
      minimum: 1
      description: Limit number of evolution entries per result (user-controlled, no maximum). Only applicable when show_evolution=true.
        Higher values provide more complete history but increase response size.
    case_sensitive:
      type: boolean
      default: false
      description: Enable case-sensitive FTS matching. Only applicable when search_mode is 'fts' or 'hybrid'. When true, query
        matches must have exact case.
    fuzzy:
      type: boolean
      default: false
      description: Enable fuzzy matching with edit distance of 1 (typo tolerance). Only applicable when search_mode is 'fts'
        or 'hybrid'. Incompatible with regex=true.
    edit_distance:
      type: integer
      default: 0
      minimum: 0
      maximum: 3
      description: Fuzzy match tolerance level (0=exact, 1=1 typo, 2=2 typos, 3=3 typos). Only applicable when search_mode
        is 'fts' or 'hybrid'. Higher values allow more typos but may reduce precision.
    snippet_lines:
      type: integer
      default: 5
      minimum: 0
      maximum: 50
      description: Number of context lines to show around FTS matches (0=list only, 1-50=show context). Only applicable when
        search_mode is 'fts' or 'hybrid'. Higher values provide more context but increase response size.
    regex:
      type: boolean
      default: false
      description: Interpret query as regex pattern for token-based matching. Only applicable when search_mode is 'fts' or
        'hybrid'. Incompatible with fuzzy=true. Enables pattern matching like 'def.*auth' or 'test_.*'.
    diff_type:
      type: string
      description: Filter temporal results by diff type (added/modified/deleted/renamed/binary). Can be comma-separated for
        multiple types (e.g., 'added,modified'). Only applicable when time_range is specified.
    author:
      type: string
      description: Filter temporal results by commit author (name or email). Only applicable when time_range is specified.
    chunk_type:
      type: string
      enum:
      - commit_message
      - commit_diff
      description: 'Filter temporal results by chunk type: ''commit_message'' searches commit messages, ''commit_diff'' searches
        code diffs. Only applicable when time_range is specified.'
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
  - query_text
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the search operation succeeded
    results:
      type: object
      description: Search results (present when success=True)
      properties:
        results:
          type: array
          description: Array of code search results
          items:
            type: object
            properties:
              file_path:
                type: string
                description: Relative path to file
              line_number:
                type: integer
                description: Line number where match found
              code_snippet:
                type: string
                description: Code snippet containing match
              similarity_score:
                type: number
                description: Semantic similarity score (0.0-1.0)
              repository_alias:
                type: string
                description: Repository where result found
              source_repo:
                type:
                - string
                - 'null'
                description: Component repository name for composite repositories. Null for single repositories. Indicates
                  which repo in a composite this result came from.
              file_last_modified:
                type:
                - number
                - 'null'
                description: Unix timestamp when file last modified (null if stat failed)
              indexed_timestamp:
                type:
                - number
                - 'null'
                description: Unix timestamp when file was indexed (null if not available)
              temporal_context:
                type:
                - object
                - 'null'
                description: Temporal metadata for time-range queries (null for non-temporal queries)
                properties:
                  first_seen:
                    type: string
                    description: ISO timestamp when code first appeared
                  last_seen:
                    type: string
                    description: ISO timestamp when code last modified
                  commit_count:
                    type: integer
                    description: Number of commits affecting this code
                  commits:
                    type: array
                    description: List of commits affecting this code
                    items:
                      type: object
                  is_removed:
                    type: boolean
                    description: Whether file was removed from current HEAD (only when include_removed=true)
                  evolution:
                    type:
                    - array
                    - 'null'
                    description: Code evolution timeline (only when show_evolution=true)
                    items:
                      type: object
        total_results:
          type: integer
          description: Total number of results returned
        query_metadata:
          type: object
          description: Query execution metadata
          properties:
            query_text:
              type: string
              description: Original query text
            execution_time_ms:
              type: integer
              description: Query execution time in milliseconds
            repositories_searched:
              type: integer
              description: Number of repositories searched
            timeout_occurred:
              type: boolean
              description: Whether query timed out
    error:
      type: string
      description: Error message (present when success=False)
  required:
  - success
---

MANDATORY REPOSITORY DISCOVERY - READ THIS FIRST:
If user does NOT explicitly specify a repository, you MUST:
1. ALWAYS search cidx-meta-global FIRST: search_code('topic', repository_alias='cidx-meta-global')
2. This returns .md files describing what each repository contains
3. Read the relevant .md to understand which repo handles the topic
4. THEN search the identified repo(s) for actual code

Skip discovery ONLY when user explicitly names a repository (e.g., "search in backend-global").

REPOSITORY SELECTION DECISION TREE:
1. User specified exact repo? -> Search that repo directly
2. User mentioned topic WITHOUT repo? -> cidx-meta-global discovery (MANDATORY - see above)
3. User wants comparison across repos? -> Use repository_alias as array + aggregation_mode='per_repo'
4. User wants best matches anywhere? -> Use repository_alias as array + aggregation_mode='global'

MULTI-REPOSITORY SEARCH:
Syntax options:
- Specific repos: repository_alias=['backend-global', 'frontend-global']
- Wildcard ALL: repository_alias='*-global' (searches all global repos)
- Pattern match: repository_alias='pch-*-global' (all repos matching pattern)
- Multiple patterns: repository_alias=['backend-*', 'frontend-*']

Aggregation strategies:
- aggregation_mode='global' (default): Returns top N results by score across ALL repos - use when finding BEST matches
- aggregation_mode='per_repo': Returns N results distributed evenly - use when COMPARING implementations

LIMIT BEHAVIOR (IMPORTANT):
- 'global' mode: limit=10 returns top 10 by score (may be 7+3+0 distribution)
- 'per_repo' mode: limit=10 with 3 repos returns 4+3+3=10 total (NOT 30!)
- Per-repo mode does NOT multiply the limit, it distributes it evenly

CACHING WITH PARALLEL QUERIES:
- Large results (>2000 chars) return preview + cache_handle
- Each result has its own cache_handle (not per-repo)
- Use get_cached_content(handle) to fetch full content page by page

ERROR HANDLING:
- Partial results supported: successful repos return results even if others fail
- Check 'errors' field in response for per-repo failures

Response formats:
- response_format='flat' (default): Results with source_repo field for attribution
- response_format='grouped': Results organized by repository

PERFORMANCE: Searching 5+ repos increases token usage proportionally. Start with limit=3-5 for multi-repo searches.

NOISE FILTERING: Use exclude_path to filter out low-value files:
- exclude_path='**/package-lock.json' (npm lockfiles)
- exclude_path='**/yarn.lock' (yarn lockfiles)
- exclude_path='**/node_modules/**' (dependencies)
- exclude_path='**/test/fixtures/**' (test data)
- exclude_path='**/*.min.js' (minified files)

Use cases:
- Microservices: Search across service repos for shared patterns
- Monorepo + libs: Search main repo with dependency repos together
- Architecture analysis: Compare implementations across codebases
- Impact analysis: Find all repos using a specific pattern/library

TL;DR: Search code using pre-built indexes. Use semantic mode for conceptual queries, FTS for exact text.

SEARCH MODE: 'authentication logic' (concept) -> semantic | 'def authenticate_user' (exact) -> fts | unsure -> hybrid

CRITICAL: Semantic search finds code by MEANING, not exact text. Results are APPROXIMATE. For exhaustive exact-text results, use FTS mode or regex_search tool.

QUICK START: search_code('user authentication', repository_alias='myrepo-global', search_mode='semantic', limit=5)

TROUBLESHOOTING: (1) 0 results? Verify alias with list_global_repos, try broader terms. (2) Temporal queries empty? Check enable_temporal via global_repo_status. (3) Slow? Start with limit=5, use path_filter.

WHEN NOT TO USE: (1) Need ALL matches with pattern -> use regex_search, (2) Exploring directory structure -> use browse_directory first.

EXAMPLE: search_code('authentication logic', repository_alias='backend-global', search_mode='semantic', limit=3)
Returns: {"success": true, "results": [{"file_path": "src/auth/login.py", "line_number": 15, "code_snippet": "def authenticate_user(username, password):\n    # Validates user credentials...", "similarity_score": 0.92, "source_repo": "backend-global"}], "total_results": 3, "query_metadata": {"query_text": "authentication logic", "execution_time_ms": 145, "repositories_searched": 1}}