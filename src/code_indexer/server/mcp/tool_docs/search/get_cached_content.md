---
name: get_cached_content
category: search
required_permission: query_repos
tl_dr: Retrieve cached content by handle with pagination support.
inputSchema:
  type: object
  properties:
    handle:
      type: string
      description: UUID4 cache handle returned from search_code results
    page:
      type: integer
      description: Page number (0-indexed). Defaults to 0 for first page.
      default: 0
      minimum: 0
  required:
  - handle
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether retrieval succeeded
    content:
      type: string
      description: Retrieved content for requested page
    page:
      type: integer
      description: Current page number (0-indexed)
    total_pages:
      type: integer
      description: Total number of pages available
    has_more:
      type: boolean
      description: Whether more pages are available after this one
    error:
      type: string
      description: Error message if retrieval failed
  required:
  - success
---

Retrieve cached content by handle with pagination support.

WORKFLOW: search_code returns snippet_cache_handle when result is truncated -> use this tool to get full content -> if has_more=true, call again with page=1, page=2, etc. until has_more=false.

CACHE EXPIRY: Handles expire after session ends. If handle expired, re-run the original search to get fresh handles.

PAGINATION: Large cached content split into pages. Use page parameter (0-indexed) to retrieve subsequent pages.