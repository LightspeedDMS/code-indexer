# Memory Retrieval

How the server attaches stored technical memories to semantic search results, which settings control it, and what
it logs.

Memory retrieval is part of the `cidx-memory` feature add-on (modular phase 4f): it runs only while `"memory"` is
in `enabled_features` ([cidx-memory](../architecture/cidx-memory.md)).

Code (under `packages/features/cidx-memory/src/cidx_memory/server/`): `memory_retrieval_pipeline.py` (filters,
ordering, hydration, nudge), `retrieval.py` (the `relevant_memories` search section `search_code` hands its
results to), `memory_candidate_retriever.py` (HNSW lookup). The core computes the query embedding once
(`src/code_indexer/server/mcp/handlers/search/query_vector.py`) and calls the section through
`server/features/search_augmenters.py`.

## What it does

Technical memories are Markdown files created with the MCP tools `create_memory`, `edit_memory` and
`delete_memory`. They live in the shared memory store of the `cidx-meta` repository
(`<golden-repos dir>/cidx-meta/memories/<id>.md`) and are indexed in its `memories` collection.

When an MCP `search_code` call runs a semantic or hybrid search against a single activated repository, the server
also looks up memories similar to the query and returns them in `query_metadata.relevant_memories` of the same
response. Clients get relevant memories without any client-side hook.

It does not run for:

- `search_mode` `fts` or `regex`, or temporal queries;
- searches of a `-global` repository, of several repositories, or of a wildcard pattern;
- REST `/api/query`.

The lookup is **on by default** while the feature is enabled. If the core could not compute the query embedding,
no lookup runs and `relevant_memories` is absent (there is no second embedding attempt).

## Pipeline

For a qualifying request, after the code search and its reranking:

1. **Query vector.** The VoyageAI embedding computed for the code search is reused; if none is available, one is
   computed (same provider and key). If that fails, a WARNING is logged and the response has no
   `relevant_memories`.
2. **Candidates.** The `memories` HNSW index returns up to `max(20, limit x memory_retrieval_k_multiplier)`
   candidates, where `limit` is the search's `limit` parameter.
3. **Voyage floor.** Candidates with a similarity below `memory_voyage_min_score` are dropped (a candidate at the
   threshold is kept).
4. **Ordering.** When the request's reranker status is `disabled`, candidates are sorted by similarity, highest
   first; otherwise their order is kept.
5. **Cohere floor.** Unless the reranker status is `disabled`, candidates whose `rerank_score` is below
   `memory_cohere_min_score` are dropped. Memory candidates are not reranked and carry no
   `rerank_score`, which counts as 0, so whenever the request's reranker status is not `disabled` every candidate
   is dropped here and the response carries the nudge entry of step 7.
6. **Bodies.** Each surviving memory's file is read from disk. A candidate with an invalid id, a path outside the
   memories directory, or an unreadable file is skipped with a WARNING.
7. **Empty result.** If nothing survives, `relevant_memories` holds one entry with `memory_id` `__empty_nudge__`,
   `is_nudge: true`, and a body that suggests recording a memory. The text comes from
   `src/code_indexer/server/mcp/prompts/memory_empty_nudge.md`.

Each returned entry carries `memory_id`, `title` (currently always empty), `hnsw_score` and `body`. When the `memories` index does not exist
yet, the lookup returns no candidates (logged once per process at INFO), so the response carries the nudge entry.

Memories come from the shared store; they are not partitioned by user.

## Settings

Runtime settings in the server database, the memory feature's config section `feature_settings.memory` (not
`config.json`). A `memory_retrieval_config` stored before the move is moved into the section on load:

| Setting | Default | Meaning |
|---------|---------|---------|
| `memory_retrieval_enabled` | `true` | Master switch. When `false`, no lookup runs and `relevant_memories` is absent |
| `memory_voyage_min_score` | `0.5` | Similarity floor of step 3 |
| `memory_cohere_min_score` | `0.4` | Rerank-score floor of step 5 |
| `memory_retrieval_k_multiplier` | `5` | Candidate pool multiplier of step 2 (positive integer) |
| `memory_retrieval_max_body_chars` | `2000` | Body length cap (positive integer); stored, but not applied: bodies are returned untruncated |

The switch and the floors are read on every request. They are edited in the Feature Settings block of the Web UI
Configuration page (or `PUT /api/admin/features/memory/config`) while the feature is enabled; the multiplier and
body cap must be at least 1, the floors between 0 and 1.

### Tuning the floors

- Unrelated memories appear: raise `memory_voyage_min_score`.
- Relevant memories are missing: lower it.

`memory_cohere_min_score` has an effect only through step 5 above.

## Log messages

| Level | Message begins with | Meaning |
|-------|---------------------|---------|
| WARNING | `Memory retrieval: could not compute query vector` | Embedding failed; no `relevant_memories` for this request |
| WARNING | `Skipping memory with invalid memory_id` | A candidate id is malformed; skipped |
| WARNING | `Skipping memory '<id>': path escapes memories directory` | A candidate path is outside the memories directory; skipped |
| WARNING | `Skipping memory '<id>': file error` | The memory file could not be read; skipped |
| INFO | `Memory HNSW index not found or empty for collection 'memories'` | No memories are indexed yet (once per process) |
