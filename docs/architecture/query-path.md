# Query Path

Maintainer reference for how a server semantic, FTS or hybrid query travels from the REST or MCP front door to the
index and back: the front-door seams, the single query-embedding chokepoint with its coalescer, concurrency governor
and query-embedding cache, the HNSW and FTS caches, fusion and reranking, and payload truncation. CLI and daemon
queries use the same `FilesystemVectorStore.search()` but none of the server-only layers described here.

All paths below are relative to `src/code_indexer/` unless they start with `src/`. The point-in-time study behind
the cache key design is archived in [query-embedding-cache-empirical-study.md](../archive/query-embedding-cache-empirical-study.md).

## Contents

- [Request flow](#request-flow)
- [Front doors](#front-doors)
- [The query-embedding chokepoint](#the-query-embedding-chokepoint)
- [Concurrency governor](#concurrency-governor)
- [Embedding coalescer](#embedding-coalescer)
- [Query-embedding cache](#query-embedding-cache)
- [HNSW and FTS index caches](#hnsw-and-fts-index-caches)
- [Fusion, reranking and filtering](#fusion-reranking-and-filtering)
- [Payload truncation](#payload-truncation)
- [Runtime settings](#runtime-settings)

## Request flow

```
REST POST /api/query                      MCP search_code
server/routers/inline_query.py            server/mcp/handlers/search/code_search.py
        |                                          |
        |  memory admission check, cluster shard forward (REST), search event context
        v                                          v
  SemanticQueryManager (server/query/semantic_query_manager.py)
  provider strategy and fusion (services/query_strategy.py)
        |
        v
  FilesystemVectorStore.search()  -- loads HNSW/id index (HNSWIndexCache) in parallel with
        |                            the query embedding
        v
  coalesced_query_embedding()  (server/services/governed_call.py)
        |-- query-embedding cache (server/services/query_embedding_cache.py)
        |-- EmbeddingCoalescer    (server/services/embedding_coalescer.py)
        '-- ProviderConcurrencyGovernor (server/services/provider_concurrency_governor.py)
                |
                v  provider HTTP call (VoyageAI / Cohere)
        |
        v
  results -> fusion -> reranking -> payload truncation -> access filtering -> response
```

## Front doors

**REST `POST /api/query`** (`server/routers/inline_query.py`, `semantic_query`). The handler:

1. Rejects the request under memory pressure (`check_query_admission()`).
2. In a cluster with sharding enabled, forwards a single-repository, non-async query to an owner node
   (`app.state.shard_router`, loop-guarded by a forward header); any routing failure serves the query locally.
3. Installs a `SearchEventContext` for search telemetry.
4. Runs semantic search through `semantic_query_manager.query_user_repositories()`; FTS opens the repository's
   Tantivy index directly (`TantivyIndexManager.open_for_search()`); hybrid runs both.
5. Applies reranking (when `rerank_query` is set), then payload truncation (`_apply_rest_semantic_truncation`,
   `_apply_rest_fts_truncation` in `server/app_helpers.py`).

The request field for the query text is `query_text`.

**MCP `search_code`** (`server/mcp/handlers/search/code_search.py`). After the same admission check and search event
context, it routes on `repository_alias`: a list goes to the omni path (`search/omni.py`), an alias ending in
`-global` to `_search_global_repo`, anything else to `_search_activated_repo` (both in `search/repo_search.py`). A
bare alias the user has not activated is promoted to its `-global` form when that golden repository is globally
active (`try_global_fallback`). Searches run through `_execute_tracked_search`, which calls the semantic query
manager and records OTEL search metrics; results then pass through `_apply_rerank_and_filter`
(`search/_shared.py`).

## The query-embedding chokepoint

Every server query embedding goes through `coalesced_query_embedding(provider, text, ...)`
(`server/services/governed_call.py`). Its callers are `FilesystemVectorStore.search()`,
`server/services/search_service.py`, `services/temporal/temporal_search_service.py`,
`services/temporal/temporal_fusion_dispatch.py` and `server/mcp/handlers/search/memory_retrieval.py`. All pass
`embedding_purpose="query"`. Cohere maps it to `input_type="search_query"`. For standard VoyageAI models no
`input_type` is sent; for `voyage-context-4` a query-purpose batch is sent to the contextualized embeddings endpoint
with `input_type="query"` (`_CONTEXTUAL_QUERY_MODELS`, `services/voyage_ai.py`).

`coalesced_query_embedding()`:

1. Resolves the cache: absent (CLI, daemon, or a server whose backend registry has no cache backend), disabled,
   mode `off`, or a normalized query over 256 characters all mean "no cache".
2. If the coalescer registry exists and `coalesce_enabled` is true, gets the coalescer for the provider's
   `:embed` lane and config digest and delegates to `EmbeddingCoalescer.submit()` (Path A). The coalescer then owns
   the cache read, the live embedding and the cache write.
3. Otherwise (Path B) calls the provider directly through `governed_query_embedding()`, wrapped by
   `_serve_with_cache()` when a cache is active.

It returns the vector plus `EmbeddingCacheMetadata`, which feeds the durable `search_embed_event` rows
(`server/services/search_embed_event_writer.py`).

## Concurrency governor

`ProviderConcurrencyGovernor` (`server/services/provider_concurrency_governor.py`) is the only limiter on serving-path
provider calls. It has four independent lanes: `voyage:embed`, `voyage:rerank`, `cohere:embed`, `cohere:rerank`. Each
lane has:

- a `ResizableLimiter` whose limit K is driven by an `AimdController` (additive increase after successes, halving on
  a 429);
- a sinbin pre-check against `ProviderHealthMonitor`: a sinbinned lane raises `ProviderSinbinnedError` without
  taking a slot.

The initial K is the runtime setting `query_provider_max_concurrency` (default 16) divided by the number of uvicorn
workers, clamped to `[coalesce_k_min, coalesce_k_max]` (defaults 8 and 32). These three seeds are read when the
governor is constructed, so a change takes effect on restart. Calls use
`execute_with_backoff(lambda: governor.execute(lane, call, acquire_timeout=30.0))`: the 429 backoff sleep happens
outside the slot. A caller that waits 30 s without getting a slot receives `GovernorBusyError`.

## Embedding coalescer

`EmbeddingCoalescer` (`server/services/embedding_coalescer.py`), one per `:embed` lane and provider config digest,
merges concurrent single-text query requests into one batch:

- The wait for a governor slot is the accumulation window: requests arriving while the dispatcher waits join the
  open batch; the attempt that obtains a slot seals the batch and makes exactly one HTTP call.
- A sealed batch never splits inside the provider: the coalescer uses the provider's own token counter and 90
  percent of its token limit, and a texts cap of `min(coalesce_max_batch_size, provider texts-per-request)`
  (`coalesce_max_batch_size` default 96).
- On success each caller gets its own vector in order; on any error every caller in the batch receives the same
  exception.
- Cache handling in `submit()`: an `on`-mode hit returns immediately without queueing or taking a slot; an
  `on`-mode miss is single-flight (concurrent requests for the same key join the owner's pending result); `shadow`
  always dispatches.

## Query-embedding cache

`QueryEmbeddingCache` (`server/services/query_embedding_cache.py`) stores query-purpose embeddings so a repeated
query skips the provider. It is built once in `server/startup/lifespan.py` from
`backend_registry.query_embedding_cache` and installed with `set_query_embedding_cache()`; it never stores
document-purpose embeddings and never stores auth-bearing data.

**Storage.** One table, `query_embedding_cache`, primary key `(cache_key, provider, model, dimension)`, value a
float32 little-endian blob. SQLite in solo mode
(`server/storage/sqlite_backends/query_embedding_cache_backend.py`), PostgreSQL in a cluster
(`server/storage/postgres/query_embedding_cache_backend.py`, migration `028_query_embedding_cache.sql`), behind the
`QueryEmbeddingCacheBackend` protocol (`server/storage/protocols/query_embedding_cache_backend.py`).

**Key.** `build_key(text, anchor_tokens, config_digest=...)`:

1. Split the query on whitespace (punctuation stays attached; case is never changed).
2. Keep the first `anchor_tokens` tokens in order; sort the remaining tokens.
3. Join with single spaces. If the result is longer than 256 characters, return `None`: the query bypasses the
   cache (no lookup, no write); the key is never truncated.
4. Return `s:<config_digest>:<normalized>`. The digest is the coalescer registry's provider digest (provider,
   endpoint, model), so two endpoints never share keys.

The effective `anchor_tokens` is the per-provider setting, or 2 when unset. A changed value logs one WARNING per
provider; rows under the old normalization stop matching and age out.

**Modes** (per provider, read on every call):

| Mode | Lookup | Provider call | Returned vector | Write |
|------|--------|---------------|-----------------|-------|
| `off` | no | always | live | none |
| `shadow` | yes, after the live call | always | live | upsert on miss; `last_used` touch on hit |
| `on` | yes | only on miss | cached on hit, live on miss | upsert on miss; `last_used` touch on hit |

An unknown mode string is treated as `shadow`. In `on` mode a cached blob whose size does not match the dimension is
treated as a miss.

**Writes and eviction.** A miss upserts the vector, then calls `prune_to_max(cap)`. The cap is
`query_embedding_cache_max_entries` (default 10000), raised to at least 100, shared by all providers, evicting by
oldest `last_used`. A hit does not write synchronously: `record_hit()` puts the touch into a process-local buffer
that a background thread flushes every 5 s with `touch_last_used_batch`; the buffer flushes early at 2048
distinct keys.

**Fail-open.** A lookup, upsert, prune or flush error logs a WARNING and the query continues on the live path.
Upsert failures are counted in `cidx.cache.embedding.write_failures`.

**Per-request bypass.** `no_embedding_cache_shortcut=true` (REST request models in `server/models/api_models.py` and
`server/models/query.py`; MCP `search_code`) skips the read but still writes the live vector. It cannot enable a
disabled cache.

**Deep-fidelity audit.** On a sampled cache hit (per-provider audit sample rate, default 0.0),
`FilesystemVectorStore.search()` runs a second HNSW search with the other vector and
`_run_deep_fidelity_audit()` (`server/services/embedding_cache_audit.py`) records the top-10 overlap on the
`search_embed_event` row. In `shadow` mode the live vector is already available; in `on` mode the sampled hit
re-embeds once. The audit never changes the served result.

**Metrics.** `EmbeddingCacheOtelMetrics` (`server/services/embedding_cache_otel_metrics.py`) publishes
`cidx.cache.embedding.*` observable gauges computed from the `search_embed_event` table over a time window
(`hit_rate`, `hits`, `misses`, `provider_calls`, `long_key`, `audit_top10_overlap`, `shadow_cosine_p50`,
`shadow_cosine_p05`, `shadow_cosine_min`, `shadow_cosine_histogram`), plus `total_entries` and `write_failures` from
the process.

## HNSW and FTS index caches

`HNSWIndexCache` and `FTSIndexCache` (`server/cache/hnsw_index_cache.py`, `server/cache/fts_index_cache.py`) keep
loaded indexes in memory between queries. They are process singletons created at startup in
`server/startup/service_init.py`; `initialize_caches(worker_count)` divides the per-node size cap
(`DEFAULT_MAX_CACHE_SIZE_MB = 4096`, `server/cache/__init__.py`) by the worker count, with a 256 MB floor per worker.
Entries expire after a TTL (default 10 minutes); the FTS cache reloads the Tantivy reader on access by default.
`FilesystemVectorStore.search()` loads the HNSW and id indexes through the cache in parallel with computing the
query embedding.

## Fusion, reranking and filtering

**Fusion.** For repositories indexed with more than one embedding provider, `services/query_strategy.py` selects a
strategy (`primary_only`, `failover`, `parallel`, `specific`); `parallel` fuses per-provider results with reciprocal
rank fusion (`fuse_rrf`, k = 60) by default.

**Reranking** (`server/mcp/reranking.py`, `_apply_reranking_sync`) runs on the fused candidate set, before
truncation. When a `rerank_query` is given the search overfetches so the reranker has more candidates, and the
reranker trims to the requested limit. Rerank calls use the governor's `:rerank` lanes.

**Order in MCP** (`_apply_rerank_and_filter`): rerank, then payload truncation, then access filtering
(`filter_query_results`, and `filter_cidx_meta_results` for `cidx-meta`), then the requested limit. The REST
handler applies the same rerank-before-truncation order.

## Payload truncation

Large result content is replaced by a preview plus a `cache_handle` so responses stay bounded. The full content is
stored in `PayloadCache` (`server/cache/payload_cache.py`), installed as `app.state.payload_cache` in
`server/startup/lifespan.py`: SQLite at `<golden_repos_dir>/.cache/payload_cache.db` in solo mode, the PostgreSQL
`payload_cache` table in a cluster, so a handle written on one node can be read on another. All oversized results of
one response are stored with one `store_batch()` call. Entries expire after `payload_cache_ttl_seconds` (default 900).
Clients fetch the rest of a search result with the MCP tool `get_cached_content`
(`server/mcp/handlers/search/cached_content.py`); truncated X-Ray results use `cidx_fetch_cached_payload`.

## Runtime settings

Every setting below is a runtime setting stored in the database; none of them is a `config.json` bootstrap key. The
query-embedding cache and `cache_config` fields are changed through the Web UI config screen. The `coalesce_*`
settings and `query_provider_max_concurrency` have no Web UI control (see
[Server settings: Not editable in the Web UI](../reference/server-settings.md#not-editable-in-the-web-ui)).

| Setting | Default | Read |
|---------|---------|------|
| `query_embedding_cache_config.query_embedding_cache_enabled` | true | every call |
| `query_embedding_cache_config.query_embedding_cache_max_entries` | 10000 | every miss |
| `query_embedding_cache_config.query_embedding_cache_voyage_mode` / `..._cohere_mode` | `shadow` | every call |
| `query_embedding_cache_config.query_embedding_cache_voyage_anchor_tokens` / `..._cohere_anchor_tokens` | unset (2) | every call |
| `query_embedding_cache_config.query_embedding_cache_voyage_audit_sample_rate` / `..._cohere_audit_sample_rate` | 0.0 | every hit |
| `coalesce_enabled` | true | every call |
| `coalesce_max_batch_size` | 96 | registry build and batch seal |
| `coalesce_k_min`, `coalesce_k_max` | 8, 32 | governor construction (restart) |
| `query_provider_max_concurrency` | 16 | governor construction (restart) |
| `cache_config.index_cache_ttl_minutes`, `cache_config.fts_cache_ttl_minutes` | 10 | applied on change |
| `cache_config.payload_preview_size_chars`, `payload_max_fetch_size_chars`, `payload_cache_ttl_seconds` | 2000, 5000, 900 | payload cache construction |

The query-embedding cache fields are defined in `QueryEmbeddingCacheConfig`, the others in `ServerConfig` and
`CacheConfig` (`server/utils/config_manager.py`). How runtime settings are stored and propagated is described in
[config-state.md](config-state.md).
