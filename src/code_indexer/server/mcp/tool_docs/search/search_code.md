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
      description: 'Multi-repo aggregation. ''global'' (default): top N by score across all repos. ''per_repo'': distributes
        N evenly across repos. IMPORTANT: limit=10 with 3 repos returns 10 TOTAL (not 30). per_repo distributes as 4+3+3=10.'
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
      description: 'Search mode: ''semantic'' for conceptual queries, ''fts'' for exact text/identifiers, ''hybrid'' runs both
        in parallel and merges via Reciprocal Rank Fusion (common hits ranked highest). Default: semantic. FTS multi-term queries
        use AND semantics (all terms must match).'
      enum:
      - semantic
      - fts
      - hybrid
      default: semantic
    language:
      type: string
      description: 'Filter by programming language name or extension (e.g., ''python'', ''py'', ''js'', ''typescript'').'
    exclude_language:
      type: string
      description: Exclude files of specified language.
    path_filter:
      type: string
      description: 'Filter by file path using glob syntax (e.g., ''*/tests/*'', ''*/src/**/*.py''). Supports *, **, ?, [seq].'
    exclude_path:
      type: string
      description: 'Exclude files matching glob pattern (e.g., ''**/node_modules/**'', ''**/package-lock.json''). Comma-separate
        multiple patterns.'
    file_extensions:
      type: array
      items:
        type: string
      description: 'Filter by file extensions (e.g., [''.py'', ''.js'']).'
    accuracy:
      type: string
      enum:
      - fast
      - balanced
      - high
      default: balanced
      description: 'Search precision: ''fast'', ''balanced'' (default), ''high''.'
    time_range:
      type: string
      description: 'Time range filter (format: YYYY-MM-DD..YYYY-MM-DD). Requires temporal index (cidx index --index-commits).
        Check enable_temporal via global_repo_status.'
    time_range_all:
      type: boolean
      default: false
      description: Search all git history. Requires temporal index.
    at_commit:
      type: string
      description: 'Code state at specific commit hash or ref (e.g., ''abc123'', ''HEAD~5''). Requires temporal index.'
    include_removed:
      type: boolean
      default: false
      description: Include removed files in results. Only for temporal queries.
    show_evolution:
      type: boolean
      default: false
      description: Include code change timeline with commit history. Requires temporal index.
    evolution_limit:
      type: integer
      minimum: 1
      description: Max evolution entries per result. Only with show_evolution=true.
    case_sensitive:
      type: boolean
      default: false
      description: Case-sensitive FTS matching. Only for fts/hybrid modes.
    fuzzy:
      type: boolean
      default: false
      description: Fuzzy matching with edit distance 1 (typo tolerance). Only for fts/hybrid. Incompatible with regex=true.
    edit_distance:
      type: integer
      default: 0
      minimum: 0
      maximum: 3
      description: Fuzzy tolerance 0-3 (0=exact). Only for fts/hybrid.
    snippet_lines:
      type: integer
      default: 5
      minimum: 0
      maximum: 50
      description: Context lines around FTS matches (0=list only, 1-50). Only for fts/hybrid.
    regex:
      type: boolean
      default: false
      description: Interpret query as regex pattern. Only for fts/hybrid. Incompatible with fuzzy=true.
    diff_type:
      type: string
      description: Filter temporal results by diff type (added/modified/deleted/renamed/binary). Comma-separate multiple.
    author:
      type: string
      description: Filter temporal results by commit author name or email.
    chunk_type:
      type: string
      enum:
      - commit_message
      - commit_diff
      description: 'Filter temporal results: ''commit_message'' or ''commit_diff''.'
    response_format:
      type: string
      enum:
      - flat
      - grouped
      default: flat
      description: 'Multi-repo result format. ''flat'' (default): single array with source_repo field per result. ''grouped'':
        results organized under results_by_repo by repository.'
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
              match_text:
                type:
                - string
                - 'null'
                description: Exact matched text from FTS engine. Only present in FTS and hybrid search modes for
                  FTS-originated results. Null or absent for pure semantic results.
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
1. ALWAYS search cidx-meta-global FIRST: search_code('topic', repository_alias='cidx-meta-global', limit=5)
2. cidx-meta-global contains AI-generated descriptions of every other repository on this server
3. INTERPRETING RESULTS: file_path='auth-service.md' means auth-service-global is relevant. Rule: strip '.md', append '-global' to get the searchable alias
4. DEPENDENCY MAP RESULTS: file_path='dependency-map/authentication.md' describes cross-repo relationships in that domain. Read the snippet to find participating repos
5. THEN search the identified repo(s) for actual code: search_code('topic', repository_alias='auth-service-global', limit=10)
6. IF cidx-meta-global NOT FOUND: fall back to list_global_repos() and search candidates directly

Skip discovery ONLY when user explicitly names a repository (e.g., "search in backend-global").

REPOSITORY SELECTION:
1. User specified exact repo? -> Search directly
2. User mentioned topic WITHOUT repo? -> cidx-meta-global discovery (MANDATORY)
3. Cross-repo comparison? -> repository_alias as array + aggregation_mode='per_repo'
4. Best matches anywhere? -> repository_alias as array + aggregation_mode='global'

SEARCH MODE: 'authentication logic' (concept) -> semantic | 'def authenticate_user' (exact) -> fts | unsure -> hybrid (runs both, merges via RRF - common hits ranked highest)

CRITICAL: Semantic search finds code by MEANING, not exact text. Results are APPROXIMATE. For exhaustive exact-text results, use FTS mode or regex_search tool.

LIMIT BEHAVIOR: limit=10 with 3 repos in 'global' mode may return 7+3+0=10. In 'per_repo' mode returns 4+3+3=10 (NOT 30 - per_repo does NOT multiply the limit).

PERFORMANCE: Start with limit=5. Each result consumes tokens proportional to code snippet size. Large fields may be truncated to snippet_preview + snippet_cache_handle (use get_cached_content to retrieve full content).

EXAMPLE: search_code('authentication logic', repository_alias='backend-global', search_mode='semantic', limit=5)