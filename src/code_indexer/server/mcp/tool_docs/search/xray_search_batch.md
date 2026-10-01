---
name: xray_search_batch
category: search
required_permission: query_repos
tl_dr: Cross-repo, multi-expression X-Ray sweep -- one background job fans out over N repositories x M scan bundles, returns a single job_id, and produces a unified tagged result.
slim_description: "Cross-repo batch X-Ray: N repos x M scan bundles in ONE background job. Each bundle has a driver_regex plus an optional evaluator_code or pattern_name. All matches are tagged with repository_alias, scan_index, and pattern_name. Large results spill to cidx_fetch_cached_payload."
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier(s): single string, list of strings, or JSON-encoded string array. Duplicates are de-duplicated before the matrix is built. Global-alias fallback: bare alias "example-repo" promotes to "example-repo-global" when the repo is globally active. Maximum 50 aliases.'
    scans:
      type: array
      description: 'Array of scan bundles (REQUIRED, non-empty, max 50). Each bundle drives one Phase-1 + Phase-2 pass per resolved repository. See bundle schema below.'
      maxItems: 50
      items:
        type: object
        properties:
          driver_regex:
            type: string
            description: 'Phase-1 regex applied to each repository. REQUIRED per bundle. Named driver_regex (not pattern) to disambiguate from pattern_name. Backed by ripgrep for content search.'
          evaluator_code:
            type: string
            description: 'Rust fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>. Mutually exclusive with pattern_name. If neither is provided the default accept-all evaluator is used.'
          pattern_name:
            type: string
            description: 'Stored xray evaluator pattern name. Mutually exclusive with evaluator_code. Resolved repo-specifically first (repo shadows __any__). Failure becomes a cell-level error, not a job abort.'
          pattern_params:
            type: object
            description: 'Parameter overrides for the resolved pattern. Only valid with pattern_name.'
          search_target:
            type: string
            enum:
              - content
              - filename
            description: 'What driver_regex applies to. Default "content".'
            default: content
          case_sensitive:
            type: boolean
            description: 'Case-sensitive Phase-1 matching. Default true.'
            default: true
          multiline:
            type: boolean
            description: 'Multi-line Phase-1 regex. Default false.'
            default: false
          pcre2:
            type: boolean
            description: 'PCRE2 engine for Phase-1. Default false.'
            default: false
        required:
          - driver_regex
    max_results:
      type: integer
      description: 'Per-cell file cap (passed to the engine as max_files). When hit: cell partial=true, max_files_reached=true. NOT a global cap across the matrix. Must be >= 1 when provided.'
      minimum: 1
    timeout_seconds:
      type: integer
      description: 'Whole-matrix wall-clock cap in seconds. Range [10, 7200]. Default 600. Wider than single-repo xray_search because the matrix covers many cells.'
      minimum: 10
      maximum: 7200
      default: 600
    await_seconds:
      type: number
      description: 'Server-side inline-wait window in seconds. Range [0, 30]. Default 0. Values > 10 emit a server warning. Multi-cell batches rarely complete inline; prefer polling via GET /api/jobs/{job_id}.'
      minimum: 0
      maximum: 30
      default: 0
  required:
    - repository_alias
    - scans
outputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: 'Single background job identifier. ALWAYS exactly one job_id (not job_ids). Poll GET /api/jobs/{job_id} for progress and results.'
    error:
      type: string
      description: 'Synchronous error code when the request is rejected before job submission.'
    message:
      type: string
      description: 'Human-readable error description.'
    scan_index:
      type: integer
      description: 'Present on per-bundle validation errors (driver_regex_required, mutually_exclusive_params, xray_evaluator_validation_failed).'
---

Cross-repo, multi-expression X-Ray sweep in ONE background job.

Runs a repos x scans matrix: every scan bundle (driver_regex + optional evaluator) is applied to every resolved repository. Each match is tagged with `repository_alias`, `scan_index`, and `pattern_name`. Returns exactly one `job_id` (not `job_ids`).

## When to Use

- Sweep a fleet of repositories for the same set of code patterns in one call.
- Avoid tracking one job_id per repo (the old xray_search omni path) -- this tool tracks one job_id for the whole matrix.
- Use xray_explore to iterate on an evaluator expression first; use xray_search_batch once you have a final set of expressions to apply across many repos.

## Key Differences from xray_search

| Feature | xray_search (omni) | xray_search_batch |
|---------|-------------------|-------------------|
| Multiple repos | YES (returns job_ids list) | YES (returns single job_id) |
| Multiple expressions | NO (one expression per call) | YES (scans array) |
| Result tagging | by repo only | by repo + scan + pattern_name |
| Timeout | [10, 600] | [10, 7200] (wider for matrix) |
| await_seconds | [0, 45] | [0, 30] (lower; batch rarely inline) |

## Naming Clarification

- `driver_regex` (bundle field): the Phase-1 regex for a specific scan bundle. Named differently from xray_search's `pattern` to avoid confusion with `pattern_name`.
- `pattern` (in matches[]): the EvalFinding label emitted by the Rust evaluator. Different field.
- `pattern_name` (bundle field): pointer to a stored xray evaluator pattern. Appears in matches[] as `pattern_name` for cross-scan grouping.

## Input Contract

Each scan bundle requires `driver_regex`. Optionally include `evaluator_code` OR `pattern_name` (mutually exclusive). When neither is provided, a default accept-all evaluator produces one finding per Phase-1 hit.

Limits:
- Max 50 repository aliases (de-duplicated before the matrix).
- Max 50 scan bundles.
- Per-cell file cap via `max_results` (engine kwarg `max_files`).

## Repo Resolution

Aliases are resolved before submitting the job:
- Direct resolution attempted first.
- Global-alias fallback: bare alias "example-repo" -> "example-repo-global" when globally active and user does not have it directly activated.
- Unresolvable aliases become `error_level="repo"` entries in errors[].
- If ALL aliases fail: synchronous error `no_repositories_resolved`.
- If SOME aliases fail: job is submitted over the resolved subset; partial=true.

## Polled Result Shape (GET /api/jobs/{job_id})

```json
{
  "matches": [
    {
      "repository_alias": "...",
      "scan_index": 0,
      "pattern_name": "catch-rethrow or null",
      "file_path": "...",
      "line_number": 5,
      "pattern": "...",
      "snippet": "..."
    }
  ],
  "errors": [
    {
      "error_level": "repo",
      "repository_alias": "...",
      "error": "repository_not_found",
      "message": "..."
    },
    {
      "error_level": "cell",
      "repository_alias": "...",
      "scan_index": 0,
      "error": "pattern_not_found",
      "message": "..."
    }
  ],
  "evaluation_errors": [
    {
      "repository_alias": "...",
      "scan_index": 0,
      "file_path": "...",
      "error_type": "EvaluatorCrash",
      "error_message": "..."
    }
  ],
  "total_repos": 2,
  "total_scans": 3,
  "total_cells": 6,
  "repos_completed": 2,
  "partial": false,
  "timeout": false,
  "cancelled": false
}
```

### errors[] error_level taxonomy

- `"repo"`: alias could not be resolved (no scan_index).
- `"cell"`: a specific (repo, scan) cell failed (scan_index required). Codes: `pattern_not_found`, `phase1_failed`, `cell_execution_error`.

### partial=true triggers

`partial=true` whenever ANY of the following occurs:
- At least one repo or cell error.
- At least one per-file evaluation error.
- Cell reports `partial=true` (e.g. max_results cap hit).
- Timeout fires.
- Job cancelled.

When `partial=true`, `BackgroundJobManager` classifies the job `completed_partial`.

## Progress Reporting

Progress advances once per REPO processed (not per cell, not per file). With 4 repos, you see 25%, 50%, 75%, 100%. On a large repo the progress may appear stalled between ticks -- this is expected.

## Large Result Caching

When the combined `matches[]`/`errors[]`/`evaluation_errors[]` JSON exceeds the server's single payload character budget (Web UI `payload_max_fetch_size_chars`, default 5000 chars), the FULL set is stored in PayloadCache as a series of whole-entry pages and the response includes:
- `cache_handle`: opaque handle for paged retrieval.
- `has_more: true`, `truncated: true`.
- `total_pages: <int>`: number of independently-fetchable cache pages the full content was split across.
- `inline_entry_truncated: true`: present only in the rare case where the single first entry alone exceeds the budget; it is then adaptively shrunk (string/list fields capped) for THIS inline response ONLY -- the cached copy stays whole and unmodified.
- `matches[]`, `errors[]`, `evaluation_errors[]`: as many WHOLE leading entries as fit the character budget inline -- a genuine character-budget-driven prefix, never a fixed count of 3, and never a partially-cut entry (unless `inline_entry_truncated` is set).
- `fetch_tool_hint`: instructions to call `cidx_fetch_cached_payload`.

Each cache page is stored as its OWN independent row and holds whole, unmodified entries -- an entry that alone exceeds the budget gets its own, necessarily oversized, page rather than being cut. Fetch every page via `cidx_fetch_cached_payload(cache_handle, page)` for `page` from 1 through `total_pages`; each page is returned WHOLE and unsliced regardless of the server's CURRENT `payload_max_fetch_size_chars` setting (a config change between store and fetch cannot corrupt or misalign a page), and parses standalone as `{"matches": [...], "errors": [...], "evaluation_errors": [...]}`. Concatenate every page's arrays, in page order, to reconstruct the full result exactly, in original order.

**Cache degradation and failure** (Bug #1928 final round): `cache_unavailable: true` means PayloadCache was down when the result was built -- you still get a bounded, honest inline page 1 (`truncated: true`, `cache_handle: null`), just no further pages to fetch until the cache is back. `success: false, error: "cache_store_failed"` means the cache was reachable but the atomic page-set write itself failed; the job result still carries every non-array metadata field the batch run already produced, alongside the failure -- only `matches[]`/`errors[]`/`evaluation_errors[]` are genuinely undeliverable.

## Cancellation

Cancel via `cancel_job(job_id)`. The worker checks for cancellation BETWEEN cells (latency <= one cell). Mid-cell hard kill of the Rust engine subprocess is NOT guaranteed (known limitation -- manager assumes multiprocessing.Process semantics). The job result reports `cancelled=true` and `partial=true` with whatever matches were collected before cancellation.

## Synchronous Error Codes

| Code | Meaning |
|------|---------|
| `auth_required` | Unauthenticated or missing query_repos permission. |
| `alias_required` | repository_alias missing or empty. |
| `scans_required` | scans missing, not a list, or empty. |
| `too_many_repositories` | More than 50 aliases provided. |
| `too_many_scans` | More than 50 scan bundles provided. |
| `timeout_out_of_range` | timeout_seconds outside [10, 7200]. |
| `await_seconds_out_of_range` | await_seconds outside [0, 30]. |
| `driver_regex_required` | A scan bundle is missing driver_regex. scan_index identifies which. |
| `mutually_exclusive_params` | Both evaluator_code and pattern_name set in one bundle. |
| `xray_evaluator_validation_failed` | Inline evaluator_code fails Rust whitelist. offending_construct and offending_line provided. |
| `no_repositories_resolved` | All aliases failed resolution. errors[] lists each failure. |

## Related

- `xray_search` -- single-expression, multi-repo fan-out returning job_ids.
- `xray_explore` -- single-repo verbose AST debug for iterating evaluators.
- `cidx_fetch_cached_payload` -- fetch oversized results by cache_handle.
- `cancel_job` -- cancel a running batch job.
- `store_xray_pattern` -- save a reusable evaluator pattern for use in pattern_name.
