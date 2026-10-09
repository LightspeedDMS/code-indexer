# Query Path and Embedding Cache Invariants

Rules for the server query path: per-query caches, the query-embedding cache, provider concurrency and the
recording of billable provider calls. Design and operator settings of the query-embedding cache:
[Query Path](../query-path.md). Index of all invariant groups: [README](README.md).

## Drift-safe query-path caching

`TTLCache` in `src/code_indexer/server/services/query_path_cache.py` is thread-safe, single-flight per key, bounded
LRU, and has an optional no-TTL mode that is still bounded.

- No staleness for static model-spec YAML (parsed once per process by `_get_voyage_model_specs` /
  `_get_cohere_model_specs`) and for keys proven immutable by `is_immutable_versioned_snapshot()`. A refresh publishes
  a new snapshot path, so it is a new key.
- Bounded staleness, at most the configured short TTL, for mutable or unproven repository paths (including the base
  clone), provider configuration and database metadata.
- Never cached: auth-bearing rows (API keys, users, MCP credentials, permissions, group membership, token
  validation), so revocation takes effect at once on every node.
- `RepoConfigCache` (`app.state.repo_config_cache`, wired in `startup/lifespan.py`) combines a no-TTL sub-cache for
  proven snapshots with a short-TTL sub-cache. Settings on `CacheConfig`: `query_path_cache_enabled`,
  `repo_config_cache_ttl_seconds` (30), `repo_config_cache_max_entries` (2048).
- `provider_config_digest()` covers every behaviour-affecting provider field (provider, model, key fingerprint, never
  the raw key, endpoint, timeouts, retry settings), so two repositories share provider state only when all match.

## Query-embedding cache

- Server only. `set_query_embedding_cache` is called only from `startup/lifespan.py`; on the CLI and daemon
  `get_query_embedding_cache()` returns `None` and the live path runs. The cache wraps `coalesced_query_embedding`
  (`src/code_indexer/server/services/governed_call.py`) outside the coalescer and governor.
- Never lowercase the key. `build_key()` (`src/code_indexer/server/services/query_embedding_cache.py`) keeps case;
  keys have the form `s:<config-digest>:<normalised-query>`; a normalised query longer than 256 characters yields
  `None`, which callers treat as a miss with no write.
- Primary key `(cache_key, provider, model, dimension)`; no repository column. Only query-purpose embeddings are
  stored; nothing auth-bearing.
- Hits buffer their `last_used` updates in process; the `qec-touch-flusher` thread writes them every 5 s with
  `touch_last_used_batch()` in one transaction. When the buffer reaches 2048 distinct keys, the hit that fills it
  flushes synchronously inline (`_TOUCH_BUFFER_MAX_SIZE`). Eviction order is therefore approximate LRU. A miss writes
  synchronously and prunes to `max_entries` (default 10000, cluster-wide; configured values below 100 are raised to
  100).
- Every cache operation fails open: WARNING and the live path, never a failed query.
- Mode per provider is `off`, `shadow` (default; always serves the live embedding and records what the cache would
  have served) or `on`; read live from `QueryEmbeddingCacheConfig` on every call. The per-request
  `no_embedding_cache_shortcut` skips the read but still writes.
- Both backends exist: `QueryEmbeddingCacheSqliteBackend` and `QueryEmbeddingCachePostgresBackend`.

## Staleness check skip for immutable snapshots

`FilesystemVectorStore(skip_staleness_check=False)` is the default. Only `FilesystemBackend.get_vector_store_client()`
sets it to `True`, in server mode, and only when `is_immutable_versioned_snapshot(project_root)` proves the path is a
snapshot. Never skip the check for a path that predicate does not prove.

## Search timeouts

- `SearchTimeoutsConfig` (`src/code_indexer/server/utils/config_manager.py`) is the only source of the MCP handler,
  provider and reranker timeouts, all runtime settings: `search_code_handler_timeout_seconds` (180),
  `default_handler_timeout_seconds` (60), `write_mode_handler_timeout_seconds` (720),
  `embedding_provider_timeout_seconds` (30), `reranker_timeout_seconds` (15), `temporal_inline_wait_seconds` (60.0).
  Do not add hardcoded timeout constants.
- `handle_tools_call` (`src/code_indexer/server/mcp/protocol.py`) applies the resolved handler timeout on both the
  async branch (`asyncio.wait_for`) and the sync branch. Tools in `_ASYNC_DISPATCH_TIMEOUT_EXEMPT_TOOLS`
  (`regex_search`, `xray_search`, `xray_explore`, `analyze_graph` and the CI log-search tools) get no dispatcher
  deadline on either branch: `regex_search` is bounded by its own search limits and ripgrep timeout, and an outer
  deadline around an executor thread cannot stop the thread anyway. Change the exempt set and its rationale together.
- `regex_search` is registered as the synchronous `handle_regex_search_sync`, so its blocking work runs in the
  executor, off the event loop.
- The remote CLI's HTTP read timeout is client-side: `api_read_timeout_seconds` in `.code-indexer/.remote-config`
  (absent means 30 s).

## Embedding coalescer and governor

- Four independent lanes in `ProviderConcurrencyGovernor` (`src/code_indexer/server/services/provider_concurrency_governor.py`):
  `voyage:embed`, `voyage:rerank`, `cohere:embed`, `cohere:rerank`. Each has its own `ResizableLimiter`
  (`resizable_limiter.py`, the single source of in-flight and high-water telemetry) and `AimdController`
  (`aimd_controller.py`): grow by one after a run of successes up to `coalesce_k_max`, halve on each 429 down to
  `coalesce_k_min`. A 429 on one lane never changes another lane.
- `provider_backoff.is_rate_limited` is the canonical 429 classifier. Providers re-raise a 429 intact on the query
  path; never wrap or mask it.
- `EmbeddingCoalescer` (`embedding_coalescer.py`): one per embed lane; the governor is the only limiter; one sealed
  batch is exactly one provider HTTP call. A batch seals before exceeding either the provider's texts-per-request cap
  or its token limit with the provider's own safety margin and token counter. The dispatcher always completes every
  caller's future, with a result or an exception.
- Every server query-embedding call passes `embedding_purpose="query"` (Cohere maps it to `search_query`). Never pass
  `None` at a query-embed call site (guard: `tests/unit/server/services/test_embedding_purpose_1104.py`).
- `coalesced_query_embedding` is the single entry point at every query site. `CoalescerRegistry`
  (`coalescer_registry.py`) is built once in `lifespan.py` after `seed_api_keys_on_startup`; the CLI, daemon and solo
  callers never build one and use a direct single call. Keep `set_coalescer_registry` / `clear_coalescer_registry`
  (guard: `tests/unit/server/startup/test_lifespan_coalescer_registry_wiring.py`).
- Runtime settings, no environment variables: `coalesce_enabled` (True, read on every call), `coalesce_max_batch_size`
  (96), `coalesce_k_min` (8) and `coalesce_k_max` (32) (read when the governor is built),
  `query_provider_max_concurrency` (16, the per-node budget). The initial per-worker limit is
  `max(k_min, budget // workers)`, clamped to `[k_min, k_max]`, with the worker count floored at 1; there is no
  cross-process state. A governor constructed with an explicit `max_concurrency` (tests) is never divided.
- `ResizableLimiter.acquire()` waits against a monotonic deadline and returns `False` when it expires, never hanging;
  the governor's `execute()` then raises `GovernorBusyError`.

## Query-embedding decision counters

`search_embed_event_writer.py` exposes two counters over the same rows (both backends, fail-open):

- `count_provider_embed_calls()`: successful embeds that were needed. Shadow-validation calls, failed attempts and
  cache bypasses are excluded; the windowed dashboard depends on this definition.
- `count_transport_calls()`: real outbound HTTP calls, adding failed/bypass direct calls and Path-B shadow hits.

Known gap, by design: a coalesced batch whose members are all shadow hits is stored with `outcome='hit'`, so
`count_transport_calls()` does not count its one real call. This happens in normal warm shadow-mode operation. It is
pinned by `tests/unit/server/storage/test_search_embed_event_backends_1293.py`.

## Embedding and reranker call tracking

Every real (not cached, not coalesced away) vendor embedding or reranker HTTP call is recorded in
`embedding_call_stats` for cost reconciliation, from both the server query path and `cidx index` children.

- Storage: `EmbeddingCallStatsSqliteBackend` / `EmbeddingCallStatsPostgresBackend`
  (`src/code_indexer/server/services/embedding_call_stats.py`), written one transaction per batch.
- Writers: `EmbeddingStatsWriter.get_active()` defaults to a no-op writer. The server installs an
  `InProcessAsyncWriter` at startup (`embedding_stats_lifespan_wiring.py`, flush interval re-read every cycle). A
  `cidx index` child installs a `CrossProcessBootstrapWriter` from `CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR` (the server
  directory, never the DSN), resolving its settings once by reading the runtime row directly; every failure there
  installs the no-op writer with a WARNING and never aborts indexing.
- Kill switch: `embedding_stats_config.enabled` is checked on every `get_active()` call by peeking at an existing
  `ConfigService` singleton; it never constructs one. No singleton means enabled.
- `instrument_call()` / `instrument_call_async()` (`embedding_call_instrumentation.py`) wrap the smallest unit that
  includes both the HTTP call and its status check, so a vendor 4xx/5xx is recorded as a failure. Recording failures
  never replace the wrapped call's result or exception. A missing API key fails before the wrapper, so it records no
  row.
- `stats_purpose_override("cache_shadow_audit")` tags the re-embed done by the on-mode cache audit.
- Retention: `EmbeddingStatsRetentionScheduler` runs one short background job every 6 hours, re-reading `enabled` and
  `retention_days` (default 90) each time.
- Read surfaces: `GET /api/admin/embedding-stats/query`, MCP `admin_embedding_stats_query`, Web UI
  `/admin/embedding-stats`; `limit` is 1-1000, `offset` at least 0.
