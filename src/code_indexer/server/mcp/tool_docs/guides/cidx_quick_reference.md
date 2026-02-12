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

CRITICAL: If you don't know which repo to search, search cidx-meta-global FIRST.

CATEGORIES: search, scip, git_exploration, git_operations, files, repo_management, golden_repos, system, user_management, ssh_keys, meta, tracing

MULTI-REPO: Pass array to repository_alias. aggregation_mode='global' for best matches, 'per_repo' for balanced representation. limit=10 with 3 repos returns 10 TOTAL (not 30).

EXAMPLE: cidx_quick_reference(category='search') for search-specific guidance.
