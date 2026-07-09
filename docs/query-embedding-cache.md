# Query-Embedding Cache

Server-side cache of query embeddings on the CIDX query path, for both providers
(VoyageAI voyage-code-3, 1024 dims; Cohere embed-v4.0, 1536 dims). The cache
stores the float32 vector produced for a query string so a repeated query can
skip the live provider embedding round-trip (200ms to 3s) and go straight to
HNSW search.

This document is the architecture reference and operator guide. For the
empirical investigation that determined the key-normalization strategy and the
rejected alternatives, see `docs/query-embedding-cache-empirical-study.md`.

Source of truth verified against the implemented code (paths under
`src/code_indexer/`):

- `server/services/query_embedding_cache.py` (key build, mode gating, fail-open,
  cap resolution, cached_total memo)
- `server/services/governed_call.py` (outside-in wrap, control-flow-by-mode,
  bypass, audit sampling gate)
- `server/storage/sqlite_backends.py` (`QueryEmbeddingCacheSqliteBackend`)
- `server/storage/postgres/query_embedding_cache_backend.py`
  (`QueryEmbeddingCachePostgresBackend`)
- `server/storage/protocols.py` (`QueryEmbeddingCacheBackend` Protocol)
- `server/services/query_embedding_cache_metrics.py`
- `server/services/embedding_cache_audit.py`
- `server/utils/config_manager.py` (`QueryEmbeddingCacheConfig`)
- `server/storage/postgres/migrations/sql/028_query_embedding_cache.sql`
- `server/startup/lifespan.py` (wiring)
- `services/cohere_embedding.py` (`_map_embedding_purpose`, the S0 fix site)

---

## 1. Architecture

### 1.1 Outside-in wrap of coalesced_query_embedding

The cache is the OUTERMOST layer of the single server query-embedding chokepoint
`coalesced_query_embedding()` in `server/services/governed_call.py`. The pre-cache
body of that function (registry-absent / coalescer-kill-switch / lane-absent /
coalesced-dispatch branches) was extracted verbatim into `_compute_live()`. The
cache wrap calls `_compute_live()` only when it needs the live vector, so the
cache intercepts BEFORE the concurrency governor and the request coalescer: a
cache hit avoids all governor and coalescer overhead, and the cache works
regardless of whether the coalescer kill-switch is on or off.

The four server query-embedding sites all call `coalesced_query_embedding()`:

- `server/services/search_service.py`
- `services/temporal/temporal_search_service.py`
- `server/mcp/handlers/search.py` (memory-query embed path)
- `storage/filesystem_vector_store.py` (the FSV `search()` chokepoint)

All gating lives inside `coalesced_query_embedding()`, so call sites are
identical on CLI and server.

### 1.2 Synchronous DB-direct store, NO RAM layer, NO batched writer

The store is synchronous DB-direct. Every cache interaction is one synchronous
backend operation:

- lookup: one synchronous `SELECT` (`backend.lookup`)
- miss: one synchronous `UPSERT` (`backend.upsert`) followed by one synchronous
  `prune_to_max` cap-enforcement call
- hit: one synchronous `last_used` touch (`backend.touch_last_used`)

There is NO per-node RAM layer and NO async/batched writer. This is a deliberate
decision, not an omission. The real workload is approximately 500 semantic
searches/day with a 30/sec ceiling, so a per-query DB round-trip is trivial.
A RAM layer remains a clean, purely additive optimization that can be added
later if QPS ever grows; it is intentionally deferred. Because there is no RAM
layer, the shared count cap (default 10000) is the single, true cluster-wide cap.

### 1.3 CLI and daemon are untouched

The cache object is process-level and is installed ONLY by server-mode lifespan
startup (`set_query_embedding_cache()` in `governed_call.py`, called from
`startup/lifespan.py`). CLI and daemon paths never install it, so
`get_query_embedding_cache()` returns None there. When the accessor is None,
`coalesced_query_embedding()` returns `live()` immediately. This is the same
"absent = first-class documented branch" pattern used by the coalescer registry.
The cache backend is sourced from `backend_registry.query_embedding_cache`; when
no backend registry exists (solo CLI mode) the cache is left unset and queries
use the live path.

### 1.4 Layer view

```
                 REST /api/query   +   MCP search_code / etc.
                                |
                                v
   SEARCH-SERVICE LAYER  (holds the HNSW index handle)
     search_service.py / temporal_search_service.py /
     mcp/handlers/search.py / storage/filesystem_vector_store.py
       - threads no_embedding_cache_shortcut down
       - hosts the S6 deep-fidelity audit at the FSV search() chokepoint
                                |
                                v  coalesced_query_embedding(provider, text, ...)
   EMBEDDING-FN LAYER  server/services/governed_call.py
       coalesced_query_embedding()   <- single server query-embed chokepoint
         cache None / not enabled / mode==off -> _compute_live()
         else: build key, qualifier, then _serve_with_cache(...)
       _compute_live() = exact post-S0 body (coalescer or direct governed call)
                                |
                                v
   STORAGE LAYER  (synchronous DB-direct; NO RAM layer, NO async writer)
     QueryEmbeddingCacheBackend Protocol
       SQLite  : QueryEmbeddingCacheSqliteBackend   (solo)
       Postgres: QueryEmbeddingCachePostgresBackend (cluster, shared)
```

### 1.5 Control-flow by mode (at the wrap)

`_serve_with_cache()` in `governed_call.py` implements the per-mode policy. The
table below is exactly what the code does.

| mode | lookup | provider call (live) | returned vector | DB write | metrics |
|------|--------|----------------------|-----------------|----------|---------|
| off | no | always | live | none | none |
| shadow | yes (sync SELECT) | always | always live | UPSERT on miss; `last_used` touch on hit | hit/miss tagged `mode=shadow`; shadow cosine on hit |
| on | yes (sync SELECT) | only on miss | cached on hit, live on miss | UPSERT on miss; `last_used` touch on hit | hit/miss tagged `mode=on` |

Important detail of the implemented shadow path: `_serve_with_cache()` calls
`live_fn()` FIRST in shadow mode, THEN does the lookup. On a shadow hit it
touches `last_used` and records the cosine between the cached blob and the live
vector; on a shadow miss it upserts the live vector. Shadow always returns the
live vector. So shadow mode never changes the served result; it only measures
what would have been served.

In on mode: a hit decodes the cached float32 bytes and returns them, skipping
`_compute_live()` entirely; a miss calls `_compute_live()`, upserts, and returns
the live vector.

The mode gate (`mode=="off"` / not-enabled) fires FIRST in
`coalesced_query_embedding()`, before the bypass flag is consulted, so the bypass
flag cannot re-enable a disabled cache.

---

## 2. Operator guide

### 2.1 The settings (QueryEmbeddingCacheConfig)

All settings are RUNTIME settings stored in the database and tunable through the
Web UI Config screen. They are NOT bootstrap keys and are NOT read from
`config.json`. The service reads them LIVE on every call via
`get_config_service().get_config().query_embedding_cache_config`, so a change
takes effect without a server restart.

The eight Web-UI-exposed settings:

| setting | default | meaning |
|---------|---------|---------|
| `query_embedding_cache_enabled` | True | Master kill switch. False = cache inert (every query uses the live path). |
| `query_embedding_cache_max_entries` | 10000 | Shared count cap. The single true cluster-wide cap. Resolved with a >= 100 safe floor. |
| `query_embedding_cache_voyage_mode` | shadow | Mode for voyage-ai: off / shadow / on. |
| `query_embedding_cache_voyage_anchor_tokens` | None (inherit global) | Anchor-token depth for voyage. |
| `query_embedding_cache_voyage_audit_sample_rate` | 0.0 | Fraction of voyage cache hits to deep-audit. Clamped to [0.0, 1.0]. |
| `query_embedding_cache_cohere_mode` | shadow | Mode for cohere: off / shadow / on. |
| `query_embedding_cache_cohere_anchor_tokens` | None (inherit global) | Anchor-token depth for cohere. |
| `query_embedding_cache_cohere_audit_sample_rate` | 0.0 | Fraction of cohere cache hits to deep-audit. Clamped to [0.0, 1.0]. |

The per-provider `anchor_tokens` fields default to None, meaning "inherit the
global fallback". The global fallback field is
`query_embedding_cache_anchor_tokens` (default 2). So the effective default
anchor depth for both providers is 2. There is also a legacy global
`query_embedding_cache_audit_sample_rate` field (default 0.0) retained for
backwards compatibility; the live audit gate reads the per-provider fields, not
the legacy global.

The per-provider mode default is `shadow`, the safe default: it measures hit
rate and fidelity without ever changing a served result. An unrecognised mode
string resolves to `shadow`.

### 2.2 The anchor dial trade-off

`build_key(text, anchor_tokens, *, config_digest)` normalizes the query and
returns a config-namespaced readable key:

1. Tokenize with `text.split()` (any whitespace run; empty tokens dropped;
   punctuation NOT stripped, it stays attached to its token).
2. Keep the first `anchor_tokens` tokens in their ORIGINAL order (the anchor
   prefix).
3. Sort the remaining tokens alphabetically (case-aware lexicographic on the
   raw Unicode code points; duplicates kept as a sorted multiset).
4. Join anchor prefix + sorted tail with single spaces -> `normalized`.
5. If `len(normalized) > 256`: return `None` (NEVER truncated; caller treats
   None as a MISS and skips lookup and write).
6. Else: return `f"s:{config_digest}:{normalized}"`.

The `s:` prefix is provably disjoint from legacy 64-hex SHA-256 keys, enabling
a passive LRU reset: old rows age out via prune_to_max without any active clear
or destructive DDL. `config_digest` is the coalescer-registry digest (provider
+ endpoint + model) so cache identity == coalescer identity: two endpoints
produce two digests = two keyspaces, closing the endpoint cross-serve gap.

Boundary behaviours: `anchor_tokens == 0` sorts ALL tokens (no anchor prefix);
`anchor_tokens >= token_count` keeps every token in original order, which is
exact-match semantics; empty/whitespace input normalizes to the empty string
(key is `f"s:{config_digest}:"`). Case is never lowercased at any step.

The dial trades cache collapse against fidelity. A higher anchor depth means
fewer tail reorderings collapse to one key (fewer hits) but the served vector is
closer to the live query's vector (higher fidelity). As the depth approaches the
token count it converges to exact-match in both fidelity and hit rate. The
empirical study found anchor-first-two (depth 2) is the recommended midpoint:
about 77% top-1 fidelity with tail-collapse hits, and the residual churn is the
harmless equally-relevant kind. That is why the default is 2.

Changing `anchor_tokens` at runtime INTENTIONALLY fragments the keyspace: old
rows keyed under the old normalization no longer match new keys and age out via
LRU. Correctness is preserved because each row's key still matches its own
normalization. When the effective `anchor_tokens` for a provider changes,
`anchor_tokens_for()` emits exactly ONE structured WARNING (per provider) so
operators understand the resulting hit-rate dip.

### 2.3 The eviction cap

`query_embedding_cache_max_entries` (default 10000) is the single true shared
cluster-wide cap, count-based, with LRU eviction by `last_used`. There is ONE
bucket for both providers (rows are distinguished by the composite PK, not by a
separate per-provider cap), so the cap is global across providers. The cap is
enforced on every miss-write: `record_miss_or_shadow()` calls
`prune_to_max(resolved_cap)` after the upsert. The resolver
`_resolve_max_entries()` applies a >= 100 safe floor (`max(configured, 100)`),
and that floor lives ONLY in the resolver, never duplicated in the backend
primitives.

Eviction order is the oldest `last_used` first, with a deterministic tie-break so
concurrent callers evict the same set:

```
ORDER BY last_used ASC, created_at ASC, cache_key ASC,
         provider ASC, model ASC, dimension ASC
```

Both backends prune to exactly the cap. The PostgreSQL backend uses a ctid-based
`DELETE ... OFFSET :max_entries` (keep the newest `max_entries` rows by that
order). The SQLite backend computes `excess = total - max_entries` and deletes
the oldest `excess` rows via `rowid IN (SELECT rowid ... ORDER BY ... LIMIT
:excess)`. Different SQL shapes, identical net effect: the table is capped to
`max_entries`.

### 2.4 Per-request bypass (no_embedding_cache_shortcut, S4)

Every REST and MCP search endpoint accepts a per-request
`no_embedding_cache_shortcut` boolean (default False), threaded down through the
search-service layer to `coalesced_query_embedding()`. The REST field is on
`SemanticSearchRequest` in `server/models/api_models.py`. When True, the cache
READ is skipped (the query is forced live), but the cache WRITE still fires
(`record_miss_or_shadow`) so future requests benefit. The not-enabled / mode==off
gates fire first, so the bypass cannot re-enable a disabled cache. The bypass is
for freshness-critical searches where a cached embedding might not reflect the
current query intent.

### 2.5 Metrics and dashboard (S5, re-sourced by Story #1295)

Story #1295 (Epic #1288 final) deleted the in-process `QueryEmbeddingCacheMetrics`
tracker entirely (per-node RAM tallies that disagreed across cluster nodes and
reset on restart). `EmbeddingCacheOtelMetrics`
(`server/services/embedding_cache_otel_metrics.py`) replaces it: every
`cidx.cache.embedding.*` instrument is now a DB-backed `ObservableGauge` whose
callback queries `WindowedCacheMetrics` (the pure aggregation layer over the
durable `search_embed_event` table, Story #1293/#1294) for `[now - window,
now)` on every OTEL export tick. There is no push path and no per-node tally
left — one source of truth, durable and cluster-aggregated by construction.

Instruments registered on the `cidx.cache` meter:

- `cidx.cache.embedding.hit_rate` (ObservableGauge) — windowed `hits/(hits+misses)`
- `cidx.cache.embedding.provider_calls` (ObservableGauge) — windowed provider embed HTTP calls
- `cidx.cache.embedding.hits` (ObservableGauge) — windowed hit count
- `cidx.cache.embedding.misses` (ObservableGauge) — windowed miss count
- `cidx.cache.embedding.long_key` (ObservableGauge) — windowed over-256-char-key bypass count
- `cidx.cache.embedding.audit_top10_overlap` (ObservableGauge) — windowed average audit overlap
- `cidx.cache.embedding.shadow_cosine_p50` / `_p05` / `_min` (ObservableGauge) — windowed shadow-mode cosine percentiles
- `cidx.cache.embedding.shadow_cosine_histogram` (ObservableGauge, one Observation per bucket) — windowed shadow-mode cosine histogram
- `cidx.cache.embedding.total_entries` (ObservableGauge) — **UNCHANGED**: still a
  cheap in-process memo (`cached_total_entries()`), NOT event-sourced (it is
  live cache STATE, not a decision event)

**BREAKING CHANGE**: `hits` and `misses` were monotonic Counters before Story
#1295; they are now windowed Gauges. Any downstream OTEL consumer that took a
`rate()`/`increase()` derivative over the old Counters must instead read the
Gauge value directly (it is already a rate over the window). `shadow_cosine`
similarly moved from a push Histogram to the percentile/histogram Gauges
above. `cidx.cache.embedding.audit_top1_match` (a Counter before Story #1295)
was explicitly REMOVED — the `search_embed_event` schema has no top1-match
column, so there is no DB source for it.

Hit-ratio is labelled per mode and never blended: shadow is a "would-serve
rate", on is a "serving rate" (the dashboard's On-Mode Hit Rate card is
additionally REQUEST-denominated via `search_event_log`, not operation-
denominated — see Story #1257).

### 2.6 Deep-fidelity audit (S6)

The deep audit answers a question the cosine cannot: do the cached vector and the
live vector return the same HNSW top-k on the real index? It runs at the
search-service layer because that is where the HNSW index handle lives
(`storage/filesystem_vector_store.py` allocates an `audit_ctx` dict, threads it
into `coalesced_query_embedding()`, and after the primary HNSW search calls
`_run_deep_fidelity_audit()` from `server/services/embedding_cache_audit.py`).

Sampling gate: on a cache HIT, if `audit_ctx` is present and the per-provider
`audit_sample_rate > 0.0` and `random.random() < rate`,
`_serve_with_cache()` populates `audit_ctx`. Misses and non-sampled hits leave
it untouched.

The on-mode sampled-re-embed behaviour is the key asymmetry:

- shadow mode: the primary search used the LIVE vector and the live vector was
  ALREADY computed, so the audit gets the live-vs-cached comparison for FREE.
  `audit_ctx` carries both `cached_blob` and `live_vec`; the second HNSW search
  uses the cached vector.
- on mode: the primary search used the CACHED vector. On the sampled fraction
  only, the audit RE-EMBEDS via one `governed_query_embedding()` provider call
  to obtain the live vector for the second search. Non-sampled on-mode hits still
  skip the provider entirely. So the only place on-mode pays a provider call is
  the sampled audit fraction.

After both searches the audit computes `top10_overlap` (size of the intersection
of the two top-10 chunk-id sets divided by the larger of the two truncated set
sizes, so identical results score 1.0 even on an index with fewer than 10 chunks).

**Re-sourced by Story #1295**: the result is no longer pushed into an
in-memory metrics object. `_record_audit_metrics()` persists `top10_overlap`
directly onto the durable `search_embed_event` row via the Story #1293 keyed
UPDATE path — `SearchEmbedEventWriter.backend.update_audit_by_key(
correlation_id, embed_key, audit_sampled=True, audit_cosine=top10_overlap)`
— keyed by the SAME `(correlation_id, embed_key)` the original decision event
was inserted under. This wires the previously-orphaned `update_audit_by_key`
(it existed since Story #1293 but had no caller) so `audit_sampled`/
`audit_cosine` actually populate on the row. `top1_match` is no longer
computed or persisted anywhere: the `search_embed_event` schema has no
top1-match column, so `cidx.cache.embedding.audit_top1_match` was explicitly
REMOVED rather than kept as an orphaned instrument (see section 2.5). The
audit is fail-open throughout: any exception (including embed_key/
correlation_id/writer being absent) is caught and logged at WARNING/DEBUG and
never affects the served result.

---

## 3. Hard invariants

These are non-negotiable. They are enforced in code and verified against it.

- NEVER lowercase the key. `build_key()` preserves case at every step. Lowercasing
  destroys CamelCase identifier matches, empirically the worst failure mode for a
  code index (it flips top-1 about 34% of the time on the evolution voyage-code-3
  index). Two queries differing only in case produce different keys.
- Composite PK is `(cache_key, provider, model, dimension)`. There is NO
  repo/collection column: a query embedding is repo-independent. The composite PK
  is the cross-provider / cross-model / cross-dimension isolation: a voyage-code-3
  1024-dim vector and a cohere embed-v4.0 1536-dim vector for the same query text
  occupy separate rows.
- Synchronous DB-direct: no RAM layer, no async/batched writer. Lookup is a sync
  SELECT, miss is a sync UPSERT + sync prune, hit is a sync last_used touch.
- The table stores ONLY query-purpose embeddings, NEVER document-purpose
  embeddings. The two have different Cohere `input_type` semantics
  (`search_query` vs `search_document`) and are not interchangeable.
- Key format: `s:<config-digest>:<normalized-query>`. The `s:`
  prefix is provably disjoint from legacy 64-hex SHA-256 keys so both keyspaces
  coexist and legacy rows age out via passive LRU (prune_to_max). `config_digest`
  is the coalescer-registry digest (provider + endpoint + model). `build_key()`
  returns `None` when the normalized-query part exceeds 256 chars; callers MUST
  treat `None` as a MISS and skip lookup and write. The key is NEVER truncated.
- The cache value is only query-string to vector. It NEVER stores auth-bearing
  data.
- The migration is additive only (`CREATE TABLE IF NOT EXISTS`), rolling-restart
  safe.
- Both backends are first-class: `QueryEmbeddingCacheSqliteBackend` (solo) and
  `QueryEmbeddingCachePostgresBackend` (cluster, shared across nodes). Both store
  the embedding as a float32 little-endian blob (SQLite BLOB / PostgreSQL BYTEA).
- `_compute_live()` is the exact post-S0 body of `coalesced_query_embedding()`.
- All cache operations are fail-open: a backend error logs a WARNING and falls
  back to the live path; it never breaks a query.

---

## 4. The S0 Cohere embedding_purpose bug

S0 is a prerequisite for the cache and was committed in isolation. It is the fix
for a latent correctness bug, independent of caching.

Root cause: the server query paths passed `embedding_purpose=None` at the
query-embed call sites (`search_service.py`, `temporal_search_service.py`, the
MCP memory embed site). The request coalescer then dropped the purpose entirely.
For Cohere, `_map_embedding_purpose()` in `services/cohere_embedding.py` maps
`"query"` to `input_type="search_query"` and ANYTHING ELSE (including None) to
`"search_document"`. So before the fix, every Cohere server query was embedded as
`search_document` instead of `search_query`.

Impact: Cohere query embeddings used the wrong input_type, degrading retrieval
quality for all Cohere server queries. (Voyage is unaffected: its API has no
`input_type`, so the purpose argument is inert for Voyage.)

Fix: pass `embedding_purpose="query"` at every server query-embed call site, and
thread the purpose through the coalescer (`embedding_coalescer.py` `submit()` ->
`do_call()` -> `get_embeddings_batch(..., embedding_purpose=...)`) so the purpose
survives coalescing. The invariant: NEVER pass `embedding_purpose=None` or omit
the argument at a query-embed call site. See the CLAUDE.md
"Query embedding_purpose invariant" section. Because the cache stores
the live vector, this fix is a prerequisite for the cache storing CORRECT Cohere
vectors.

---

## 5. Notes on doc-vs-code reconciliation

For completeness, two places where the design layout document and a config
docstring differ from the implemented code; the code is authoritative:

- The design layout `0NN_query_embedding_cache.sql` placeholder resolved to
  `028_query_embedding_cache.sql`.
- The `QueryEmbeddingCacheConfig` docstring in `config_manager.py` still says the
  S6 audit "logic not yet wired". It IS wired: the audit sampling gate lives in
  `_serve_with_cache()` and the audit itself in `embedding_cache_audit.py`,
  reachable from the FSV search chokepoint. This guide describes the wired
  behaviour.
- The design layout shows the SQLite prune as `DELETE ... LIMIT -1 OFFSET
  :max_entries`. The implemented SQLite backend instead deletes the oldest
  `excess = total - max_entries` rows via `LIMIT :excess`. Net effect is the same
  cap; the guide documents the implemented form.
