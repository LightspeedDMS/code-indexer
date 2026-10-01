---
name: cidx_fetch_cached_payload
category: search
required_permission: query_repos
tl_dr: Retrieve the full payload for a truncated xray_search, xray_explore, or analyze_graph result using the cache_handle returned when a result was too large to return inline.
slim_description: "Retrieve the full payload for a truncated xray/analyze_graph result using its cache_handle."
inputSchema:
  type: object
  properties:
    cache_handle:
      type: string
      description: 'Opaque cache handle returned in the cache_handle field of a truncated xray_search, xray_explore, xray_search_batch, or analyze_graph result. Also returned in the fetch_tool_hint field of the truncated response.'
    page:
      type: integer
      description: 'Page number (1-indexed). Defaults to 1 for first page. Use has_more field in the response to determine if additional pages exist. For a pages-v1 (xray_search/xray_explore/xray_search_batch/analyze_graph) cache_handle: must be a real integer >= 1, OR a decimal-digit string such as "2" (coerced to int) -- a float, zero, a negative value, a bool, or any other non-digit string returns error "invalid_page", with NO clamping. For any OTHER (legacy) cache_handle, page keeps the tool''s original lenient behavior: it is coerced via int(page or 1) and clamped up to 1 if the result is less than 1, so page=0 (or any other falsy/coercible value) silently becomes page 1 rather than erroring.'
      default: 1
      minimum: 1
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
      description: 'Payload content for the requested page. For a truncated xray_search/xray_explore (matches[]/evaluation_errors[]), xray_search_batch (matches[]/errors[]/evaluation_errors[]), or analyze_graph (findings[]/refine[]) result, each page is an INDEPENDENTLY PARSEABLE JSON object -- e.g. {"matches": [...], "evaluation_errors": [...]} -- holding a whole-entry slice (json.loads works on any single page by itself); concatenate every page''s array fields, in page order, to reconstruct the full arrays. For other cache handles (plain-text content such as file content or git diffs), content is a raw text chunk and pages must be concatenated to reconstruct the full text.'
    page:
      type: integer
      description: 'Current page number (1-indexed).'
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

Retrieve the full payload for a truncated xray_search, xray_explore, xray_search_batch, or analyze_graph result.

When an xray_search, xray_explore, xray_search_batch, or analyze_graph job produces a result larger than the server's single payload character budget (configurable via Web UI payload_max_fetch_size_chars, default 5000 chars), the result is truncated to as many WHOLE leading entries as fit that budget inline -- never a fixed count, never a partially-cut entry (the sole exception: when even the single first entry alone exceeds the budget, it is adaptively shrunk for the INLINE response only, flagged `inline_entry_truncated`; the cached copy stays whole). The truncated response includes a cache_handle and a fetch_tool_hint field naming this tool.

Use this tool to retrieve the full content using that handle. For very large results, paginate by incrementing the page parameter (1-indexed) until has_more is false.

## Page Format (pages-v1 manifest)

A truncated result's cache_handle points at a small "pages-v1" manifest record -- NOT the data itself -- that lists the handles of every page holding the full, whole-entry data, all written together with the manifest in ONE atomic batch (so pages and the manifest always expire together; a page-set write that fails never leaves a dangling handle). Fetching page N resolves the manifest, looks up that page's own handle, and returns ITS content whole and unsliced -- every page is stored as its own independent cache row holding whole, unmodified entries; an entry that alone exceeds the budget gets its own, necessarily oversized, page rather than being cut. Each page's `content` is an independently parseable JSON object holding a whole-entry slice of the truncated array fields (e.g. `{"matches": [...], "evaluation_errors": [...]}`, `{"matches": [...], "errors": [...], "evaluation_errors": [...]}`, or `{"findings": [...], "refine": [...]}`) -- parse each page on its own, then concatenate every page's array fields, in page order, to reconstruct the full arrays exactly, in original order. This holds regardless of how many pages a result spans, AND regardless of the server's CURRENT payload_max_fetch_size_chars setting -- a config change made after the result was cached cannot corrupt or misalign an already-stored page. A cache_handle produced before this pagination format shipped is still served correctly, via the older character-windowed retrieval it was always stored under.

## Workflow

1. Run xray_search, xray_explore, or analyze_graph and poll the job to COMPLETED.
2. If the result contains has_more: true and a cache_handle field, the result was truncated.
3. Call cidx_fetch_cached_payload with that cache_handle to retrieve the full payload.
4. If the response has has_more: true, call again with page=2, page=3, etc. until has_more: false.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| cache_handle | str | yes | -- | Opaque cache handle from the truncated xray result's cache_handle field. |
| page | int | no | 1 | Page number (1-indexed). Call repeatedly until has_more: false. |

## Error Codes

| Error Code | Meaning |
|------------|---------|
| auth_required | User is not authenticated or lacks query_repos permission. |
| missing_handle | The cache_handle parameter was not provided, or was empty/falsy (None, false, 0, an empty list, etc.). |
| invalid_page | Only for a pages-v1 (xray_search/xray_explore/xray_search_batch/analyze_graph) cache_handle: page is not a real integer >= 1 and not a decimal-digit string -- a float, zero, a negative value, a bool, or a non-digit string. Never clamped or coerced for these handles. A LEGACY (non-pages-v1) cache_handle never returns this error -- an invalid page value there is silently clamped instead (see the page parameter description above). |
| malformed_cache_entry | cache_handle is a pages-v1 manifest handle but its stored content fails strict validation -- a real server-side data problem, re-run the original job for a fresh handle. |
| cache_expired | The cache handle has expired or does not exist. Re-run the original xray_search or xray_explore to get a fresh handle. |
| cache_unavailable | The PayloadCache service is not configured/available on this server. Distinct from cache_expired: the handle itself may be fine, but there is currently nothing to fetch it from -- retry once the cache service is back. |

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
  "page": 2
}
```

## Related

- See `xray_search` for the two-phase AST-aware search that may produce truncated results.
- See `xray_explore` for the debug-mode search variant that also may produce truncated results.
- See `xray_search_batch` for the multi-repo batch search variant that may also produce truncated `matches[]`/`errors[]`/`evaluation_errors[]` results, using the same character-budget truncation and page format.
- See `analyze_graph` for the whole-repository graph analysis that may produce truncated `findings[]`/`refine[]` results, using the same character-budget truncation and page format as xray_search/xray_explore.
- See `get_cached_content` for fetching truncated search_code results (a DIFFERENT tool with a 0-indexed pagination convention -- it rejects a pages-v1 manifest handle with a clear message pointing back here).
