---
name: get_cached_content
category: search
required_permission: query_repos
tl_dr: 'Retrieve cached content by handle with pagination support.


  WORKFLOW: search_code returns snippet_cache_handle when result is truncated -> use
  this tool to get full content -> if has_more=true, call again with page=1, page=2,
  etc.'
---

Retrieve cached content by handle with pagination support.

WORKFLOW: search_code returns snippet_cache_handle when result is truncated -> use this tool to get full content -> if has_more=true, call again with page=1, page=2, etc. until has_more=false.

CACHE EXPIRY: Handles expire after session ends. If handle expired, re-run the original search to get fresh handles.

PAGINATION: Large cached content split into pages. Use page parameter (0-indexed) to retrieve subsequent pages.