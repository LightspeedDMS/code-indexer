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

TL;DR: Retrieve cached content by handle with pagination support. USE CASE: Fetch full content when search results return truncated previews with cache_handle. WHEN TO USE: After search_code returns results with cache_handle and has_more=true, use this tool to retrieve the complete content page by page. WORKS WITH PARALLEL QUERIES: When multi-repo search returns results with cache_handles, each result has its own independent handle (not per-repo). Call this tool separately for each handle you want to expand. WORKFLOW: (1) search_code returns results with has_more=true and cache_handle. (2) Call get_cached_content(handle, page=0) to get first chunk. (3) If response has_more=true, call with page=1, page=2, etc. (4) Repeat until has_more=false. PAGINATION: Content is split into pages (default 5000 chars/page). Use page parameter (0-indexed) to retrieve subsequent pages. RESPONSE: Returns content, page number, total_pages, and has_more flag. CACHE EXPIRY: Handles expire after 15 minutes (configurable). If handle expired, re-run the search to get fresh handles. RELATED TOOLS: search_code, regex_search, git_log (all return cache_handle for large results).