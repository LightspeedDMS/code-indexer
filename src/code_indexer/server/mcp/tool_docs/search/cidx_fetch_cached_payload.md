---
name: cidx_fetch_cached_payload
category: search
required_permission: query_repos
tl_dr: Retrieve the full payload for a truncated xray_search or xray_explore result using the cache_handle returned when a result was too large to return inline.
inputSchema:
  type: object
  properties:
    cache_handle:
      type: string
      description: 'Opaque cache handle returned in the cache_handle field of a truncated xray_search or xray_explore result. Also returned in the fetch_tool_hint field of the truncated response.'
    page:
      type: integer
      description: 'Page number (0-indexed). Defaults to 0 for first page. Use has_more field in the response to determine if additional pages exist.'
      default: 0
      minimum: 0
  required:
    - cache_handle
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: 'True when retrieval succeeded.'
    content:
      type: string
      description: 'Full payload content for the requested page.'
    page:
      type: integer
      description: 'Current page number (0-indexed).'
    total_pages:
      type: integer
      description: 'Total number of pages available for this handle.'
    has_more:
      type: boolean
      description: 'True when additional pages are available after this one.'
    error:
      type: string
      description: 'Error code when retrieval failed.'
    message:
      type: string
      description: 'Human-readable description of the error.'
  required:
    - success
---

Retrieve the full payload for a truncated xray_search or xray_explore result.

When an xray_search or xray_explore job produces a result larger than the server's payload preview cap (configurable via Web UI payload_preview_size_chars, default ~2000 chars), the result is truncated. The truncated response includes a cache_handle and a fetch_tool_hint field naming this tool.

Use this tool to retrieve the full content using that handle. For very large results, paginate by incrementing the page parameter (0-indexed) until has_more is false.

## Workflow

1. Run xray_search or xray_explore and poll the job to COMPLETED.
2. If the result contains has_more: true and a cache_handle field, the result was truncated.
3. Call cidx_fetch_cached_payload with that cache_handle to retrieve the full payload.
4. If the response has has_more: true, call again with page=1, page=2, etc. until has_more: false.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| cache_handle | str | yes | -- | Opaque cache handle from the truncated xray result's cache_handle field. |
| page | int | no | 0 | Page number (0-indexed). Call repeatedly until has_more: false. |

## Error Codes

| Error Code | Meaning |
|------------|---------|
| auth_required | User is not authenticated or lacks query_repos permission. |
| missing_cache_handle | The cache_handle parameter was not provided. |
| cache_expired | The cache handle has expired or does not exist. Re-run the original xray_search or xray_explore to get a fresh handle. |

## Cache Expiry

Handles expire when the server session ends or after a server-configured TTL. If you receive cache_expired, re-run the original xray_search or xray_explore job to obtain a fresh handle.

## Examples

**Fetch first page of a truncated result:**
```json
{
  "cache_handle": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}
```

**Fetch second page:**
```json
{
  "cache_handle": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "page": 1
}
```

## Related

- See `xray_search` for the two-phase AST-aware search that may produce truncated results.
- See `xray_explore` for the debug-mode search variant that also may produce truncated results.
- See `get_cached_content` for fetching truncated search_code results (different tool, same pagination pattern).
