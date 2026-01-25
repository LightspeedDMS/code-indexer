---
name: cidx_quick_reference
category: guides
required_permission: query_repos
tl_dr: Get quick reference for CIDX MCP tools with decision guidance.
quick_reference: true
inputSchema:
  type: object
  properties:
    category:
      type:
      - string
      - 'null'
      enum:
      - search
      - scip
      - git_exploration
      - git_operations
      - files
      - repo_management
      - golden_repos
      - system
      - user_management
      - ssh_keys
      - meta
      - null
      default: null
      description: 'Optional category filter. null/omitted returns all tools. Options: search, scip, git_exploration, git_operations,
        files, repo_management, golden_repos, system, user_management, ssh_keys, meta.'
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the operation succeeded
    total_tools:
      type: integer
      description: Total number of tools returned
    category_filter:
      type:
      - string
      - 'null'
      description: Category filter applied (null if showing all)
    tools:
      type: array
      description: List of tool summaries
      items:
        type: object
        properties:
          name:
            type: string
            description: Tool name
          category:
            type: string
            description: Tool category
          summary:
            type: string
            description: TL;DR summary from tool description
          required_permission:
            type: string
            description: Permission required to use this tool
        required:
        - name
        - category
        - summary
        - required_permission
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
  - total_tools
  - tools
---

TL;DR: Get quick reference for CIDX MCP tools with decision guidance.

FIRST THING TO KNOW - REPOSITORY DISCOVERY:

CRITICAL: If you don't know which repository contains the code you're looking for, DO NOT GUESS. Follow this workflow:

1. Search cidx-meta-global FIRST:
   search_code(query_text='your topic', repository_alias='cidx-meta-global', limit=5)
   This returns .md files describing what each repository contains.

2. Read the returned .md files to identify relevant repositories.
   NOTE: Your topic may exist in MULTIPLE repositories. Collect all matches.

3. THEN search for actual code based on discovery results:
   - ONE relevant repo found -> Single-repo search (see SINGLE-REPO section)
   - MULTIPLE relevant repos found -> Multi-repo search (RECOMMENDED, see MULTI-REPO section)

   Example with multiple repos discovered:
   search_code(query_text='your topic', repository_alias=['backend-global', 'frontend-global'], aggregation_mode='global', limit=10)

WHY THIS MATTERS: cidx-meta-global is the "index of indexes" - it contains descriptions of all repositories. Searching it first prevents wasted time searching wrong repos and identifies ALL relevant codebases.

WORKFLOW DECISION TREE:

Q: Do you know which repository to search?
  YES -> Use single-repo search: search_code(query_text='topic', repository_alias='specific-repo-global')
  NO -> Search cidx-meta-global FIRST (see above)

Q: cidx-meta returned multiple relevant repositories?
  YES -> Use multi-repo search (RECOMMENDED) - single query, unified results, consistent scoring
  NO (only one repo) -> Use single-repo search

Q: Need to explore or discover a concept across the codebase?
  YES -> Use multi-repo search with aggregation_mode='global' (RECOMMENDED for discovery)

Q: Need to compare implementations across repositories?
  YES -> Use multi-repo search with aggregation_mode='per_repo' (ensures each repo represented)

Q: Need best matches regardless of source?
  YES -> Use multi-repo search with aggregation_mode='global'

SINGLE-REPO SEARCH:
search_code(query_text='authentication', repository_alias='backend-global', limit=5)
- Use repository_alias as a string
- Best for deep-diving into one codebase

MULTI-REPO SEARCH (RECOMMENDED FOR CROSS-REPO EXPLORATION):

WHY MULTI-REPO IS PREFERRED:
- Single query instead of N sequential queries (faster, less overhead)
- Consistent scoring across all repositories (apples-to-apples comparison)
- Unified results with source_repo attribution (easy to see distribution)
- Parallel execution on server side (performance optimized)
- Partial failure handling (one repo down doesn't block others)

DO NOT search repos one-by-one when you need cross-repo results. Use multi-repo.

SYNTAX OPTIONS:
- Specific repos: repository_alias=['backend-global', 'frontend-global']
- Wildcard ALL: repository_alias='*-global' (searches all global repos)
- Pattern match: repository_alias='pch-*-global' (all repos matching pattern)

EXAMPLE:
search_code(query_text='authentication', repository_alias=['backend-global', 'api-global'], aggregation_mode='global', limit=10)

TOOLS SUPPORTING MULTI-REPO: search_code, regex_search, git_log, git_search_commits, list_files

AGGREGATION MODES:
- 'global' (default): Returns top N results by score across all repos (RECOMMENDED for discovery/exploration)
- 'per_repo': Distributes N evenly across repos (best for comparison)

LIMIT MATH (IMPORTANT):
limit=10 with 3 repos in per_repo mode returns 4+3+3=10 TOTAL results, NOT 30.
In global mode, returns top 10 by score regardless of source (may be 7+3+0 distribution).

CACHING:
Large results (>2000 chars) return preview + cache_handle.
Each result has its own handle (not per-repo).
Use get_cached_content(handle) to fetch full content.

ERROR HANDLING:
Partial results supported. Failed repos appear in 'errors' field, successful repos return results.

PERFORMANCE TIP:
Start with limit=3-5 for multi-repo searches. Token usage scales with number of repos.

TOOL CATEGORIES:
search, scip, git_exploration, git_operations, files, repo_management, golden_repos, system, user_management, ssh_keys, meta

Use category filter to narrow results:
cidx_quick_reference(category='search') -> returns search tools with summaries
