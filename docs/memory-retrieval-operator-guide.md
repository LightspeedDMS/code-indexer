# Memory Retrieval Operator Guide

Story #883 — Semantic-Triggered Parallel Memory Retrieval for Hookless MCP Clients

---

## What This Feature Does

When a client calls `search_code` with `search_mode` set to `semantic` or `hybrid`, the server
runs a parallel memory retrieval pipeline that searches the stored technical memory store for
entries relevant to the query. The results are injected into the `relevant_memories` field of
the search response.

This allows hookless MCP clients (clients that cannot intercept MCP responses to inject memory
context) to receive relevant historical technical knowledge automatically, without requiring
any client-side changes.

The feature is disabled by default. Operators must explicitly enable it via the configuration
screen.

---

## Pipeline Execution Order

For each qualifying search request:

1. The query vector is computed via VoyageAI using the same `VOYAGE_API_KEY` as code search.
2. HNSW candidate retrieval runs from the memory store for the authenticated user.
3. Voyage floor filter: candidates below `memory_voyage_min_score` are dropped.
4. Relevant memory assembly: candidates are hydrated with context fields.
5. Ordering: if the Cohere reranker is active, candidates are sorted by rerank score descending;
   otherwise by HNSW score descending.
6. Cohere floor filter: candidates below `memory_cohere_min_score` are dropped (only when
   the reranker is active).
7. Body hydration: each surviving candidate's full body text is read from disk.
8. If no candidates survive all filters, a nudge entry is injected prompting the client to
   use `store_technical_memory` to begin building a memory store.

---

## Configuration Keys

All keys live in the `memory_retrieval_config` object in the server runtime configuration
(managed via the Web UI Config Screen, not `config.json`).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `memory_retrieval_enabled` | bool | `false` | Master on/off switch. Set to `true` to activate the pipeline. |
| `memory_retrieval_limit` | int | `5` | Maximum number of memory candidates passed to HNSW retrieval. |
| `memory_voyage_min_score` | float | `0.75` | Minimum HNSW similarity score for a memory candidate to survive the first filter. |
| `memory_cohere_min_score` | float | `0.5` | Minimum Cohere rerank score for a candidate to survive the second filter (only applied when the reranker is active). |
| `memory_reranker_enabled` | bool | `false` | Whether to apply the Cohere reranker to memory candidates. |

---

## Kill Switch

Set `memory_retrieval_enabled` to `false` in the Web UI Config Screen. The change takes effect
immediately on the next search request — no server restart required.

When the kill switch is off:
- The HNSW retrieval step is skipped entirely.
- No VoyageAI call is made.
- The `relevant_memories` field is absent from the response.

---

## Floor Tuning

### Voyage Floor (`memory_voyage_min_score`)

Controls how strict the initial HNSW similarity filter is. Higher values mean only very close
semantic matches survive. Lower values allow more candidates through.

Typical starting range: 0.70 to 0.80. If users report that unrelated memories appear in
results, raise this value. If relevant memories are being dropped, lower it.

### Cohere Floor (`memory_cohere_min_score`)

Only active when `memory_reranker_enabled` is `true` and the server-level Cohere reranker
is configured. Controls how strictly reranked candidates are filtered.

Typical starting range: 0.40 to 0.60. Adjust in the same direction as the Voyage floor:
raise to eliminate noise, lower to recover dropped relevant memories.

---

## Empty-State Nudge Behavior

When the pipeline produces zero surviving candidates after all filters, the server injects a
single synthetic entry into `relevant_memories` with `memory_id` set to `__empty_nudge__`
and `is_nudge` set to `true`. The body text guides the client to use `store_technical_memory`
to begin building the memory store.

The nudge text is loaded from:

```
src/code_indexer/server/mcp/prompts/memory_empty_nudge.md
```

Operators can edit this file to customize the message without touching Python code. The
loaded text is cached for the lifetime of the process (one load per process startup).

---

## Parallel Execution Notes

The memory retrieval pipeline runs concurrently with the code search query in a thread pool.
The code search results and memory results are merged before the response is returned.

If the memory pipeline raises an unhandled exception, it is logged as a WARNING and the
search response is returned without `relevant_memories` rather than failing the entire
request.

If VoyageAI is unreachable or returns an error when computing the query vector, a WARNING
is logged and the memory pipeline is skipped for that request (same behavior as kill switch
off for that request only).

---

## Per-User Isolation

Memory candidates are scoped to the authenticated user's `username`. One user's memories are
never surfaced to another user. This is enforced at the HNSW retrieval step.

---

## Rollback Procedure

1. Set `memory_retrieval_enabled` to `false` in the Web UI Config Screen.
2. No server restart is required.
3. The feature is fully disabled. No pipeline code runs after the kill switch is off.

If a complete code rollback is needed (for example, due to a bug in the pipeline itself):
deploy the previous version to the branch and the auto-updater will restart the service.
No database migrations are involved — this feature uses no new tables.

---

## Supported Search Modes

Memory retrieval is triggered only for the following `search_mode` values:

- `semantic`
- `hybrid`

It is explicitly skipped for `fts` (full-text search) mode, because FTS queries are keyword
lookups that do not produce a meaningful query vector for HNSW retrieval.

---

## Log Messages

| Level | Message Pattern | Meaning |
|-------|-----------------|---------|
| WARNING | `Memory retrieval: could not compute query vector` | VoyageAI call failed; pipeline skipped for this request |
| WARNING | `Memory body hydration: invalid memory_id` | A candidate had a malformed memory_id; candidate skipped |
| WARNING | `Memory body hydration: path traversal attempt` | A candidate memory_id failed path confinement check; candidate skipped |
| WARNING | `Memory body hydration: failed to read` | Disk read error for a candidate's file; candidate skipped |
| INFO | (search response includes `relevant_memories`) | Normal operation |

---

## File Locations

| Path | Purpose |
|------|---------|
| `src/code_indexer/server/mcp/memory_retrieval_pipeline.py` | Pipeline orchestration, HNSW retrieval, filters, body hydration |
| `src/code_indexer/server/mcp/handlers/search.py` | Handler integration, `_run_memory_retrieval`, `_compute_memory_query_vector` |
| `src/code_indexer/server/mcp/prompts/memory_empty_nudge.md` | Nudge text (editable without Python changes) |
| `tests/unit/server/mcp/test_search_memory_retrieval.py` | Unit test suite (20 tests) |

*Recorded 2026-04-22 (Story #883)*
