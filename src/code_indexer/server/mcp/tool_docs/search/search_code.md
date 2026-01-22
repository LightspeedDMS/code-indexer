---
name: search_code
category: search
required_permission: query_repos
tl_dr: Search code using pre-built indexes.
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