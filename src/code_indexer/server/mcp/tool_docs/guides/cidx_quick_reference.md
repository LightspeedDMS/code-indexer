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

Quick reference card for CIDX tools with decision guidance. Returns dynamic content based on optional category filter.

REPOSITORY DISCOVERY (cidx-meta-global):
cidx-meta-global is a synthetic repository that contains AI-generated markdown descriptions of every other repository registered on this server. Each file in cidx-meta-global is named after a repository (e.g., 'auth-service.md' describes the auth-service-global repository). It also contains a dependency-map/ subdirectory with cross-repository architectural analysis organized by domain.

WHEN TO USE: Before searching for code when you don't know which repository contains the topic. Skip only when the user explicitly names a repository.

DISCOVERY WORKFLOW:
1. search_code(query_text='your topic', repository_alias='cidx-meta-global', limit=5) -- find relevant repos
2. Results will have file_path like 'auth-service.md' or 'dependency-map/authentication.md'
3. For repo description files: strip '.md' from file_path and append '-global' to get the repository alias (e.g., 'auth-service.md' -> 'auth-service-global')
4. For dependency-map files: read the content to find which repos participate in that domain
5. search_code(query_text='your topic', repository_alias='auth-service-global', limit=10) -- search the identified repo

IF cidx-meta-global IS NOT AVAILABLE: Fall back to list_global_repos() to see all repos, then search the most likely candidates.

CATEGORIES: search, scip, git_exploration, git_operations, files, repo_management, golden_repos, system, user_management, ssh_keys, meta, tracing

MULTI-REPO: Pass array to repository_alias. aggregation_mode='global' for best matches, 'per_repo' for balanced representation. limit=10 with 3 repos returns 10 TOTAL (not 30).

SEARCH MODE: 'authentication logic' (concept) -> semantic | 'def authenticate_user' (exact) -> fts | unsure -> hybrid

EXAMPLE: cidx_quick_reference(category='search') for search-specific guidance.
