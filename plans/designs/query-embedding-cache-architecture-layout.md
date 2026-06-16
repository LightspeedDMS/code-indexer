# Query-Embedding Cache — Cross-Cutting Architecture Layout

Server-side cache of query embeddings on the CIDX query path, both providers
(VoyageAI voyage-code-3, Cohere embed-v4.0). This document maps every touch
point: existing components to MODIFY (with injection points) and new components
to INSERT, plus the request/control flow and the story-to-component matrix.

Design posture (post-review): the store is SYNCHRONOUS DB-DIRECT — one
synchronous backend SELECT on lookup, one synchronous UPSERT on a miss, one
synchronous `last_used` touch on a hit. There is NO per-node RAM layer and NO
async/batched writer. Rationale: actual workload is ~500 semantic searches/day
with a 30/sec ceiling, so a per-query DB round-trip is trivial. A RAM layer is
deferred and may be added later (purely additive) if QPS ever grows. The shared
count cap (default 10000) is therefore the SINGLE, TRUE cluster-wide cap.

Paths are relative to `src/code_indexer/`. Line numbers are indicative and must
be re-confirmed at implementation time.

---

## 1. Layered view (where everything lives)

```
                          CLIENT (REST + MCP front door)
  ┌─────────────────────────────────────────────────────────────────────────┐
  │ API LAYER                                                                 │
  │   REST: /api/query           server/app.py routes -> search handlers      │
  │   MCP : search_code, etc.    server/mcp/handlers/search.py                │
  │   [MODIFY S4] add per-request `no_embedding_cache_shortcut: bool=False`   │
  │     (keyword default) to:                                                  │
  │     - server/models/api_models.py :: SemanticSearchRequest                │
  │     - server/mcp/tool_docs/search/search_code.md (+ embedding-bearing     │
  │       tool docs) ; gate: tools/verify_tool_docs.py                        │
  └───────────────────────────────┬───────────────────────────────────────────┘
                                   │ bypass flag threaded down
  ┌───────────────────────────────▼───────────────────────────────────────────┐
  │ SEARCH-SERVICE LAYER  (holds the HNSW index handle)                        │
  │   server/services/search_service.py        (~494)                         │
  │   services/temporal/temporal_search_service.py  (~438)                    │
  │   server/mcp/handlers/search.py            (memory query embed ~514)       │
  │   storage/filesystem_vector_store.py :: search()  (~2498-2512)            │
  │   [INSERT S6] 3b deep-fidelity audit executes HERE (needs index handle):  │
  │     on a sampled hit -> 2nd HNSW with cached vec vs the live result.       │
  │     SHADOW: the live vec already exists (free). ON: the sampled hit        │
  │     RE-EMBEDS one provider call (sampled fraction only); non-sampled       │
  │     on-hits still skip the provider.                                       │
  └───────────────────────────────┬───────────────────────────────────────────┘
                                   │ coalesced_query_embedding(provider, text, bypass)
  ┌───────────────────────────────▼───────────────────────────────────────────┐
  │ EMBEDDING-FN LAYER   server/services/governed_call.py                      │
  │   coalesced_query_embedding()  <-- SINGLE server query-embedding chokepoint│
  │   (CLI/solo bypass: get_coalescer_registry()/get_query_embedding_cache()   │
  │    is None -> direct path, so "CLI/daemon untouched" is automatic)         │
  │                                                                            │
  │   ╔══════ [INSERT S1] QueryEmbeddingCache.wrap()  (OUTSIDE wrap) ════════╗ │
  │   ║ 1. cache None OR not enabled OR mode==off OR bypass-read -> live      ║ │
  │   ║ 2. key = build_key(text, anchor_tokens[provider])  [S2, CASE-KEPT]   ║ │
  │   ║ 3. synchronous backend SELECT (DB-direct; no RAM layer)              ║ │
  │   ║      HIT & mode==on     -> sync last_used touch; return cached vec   ║ │
  │   ║                            (SKIP provider)                            ║ │
  │   ║      HIT & mode==shadow -> sync last_used touch; record hit +        ║ │
  │   ║                            3a cos(cached,live); fall through          ║ │
  │   ║      MISS               -> fall through                               ║ │
  │   ║ 4. live = _compute_live()   (the EXACT post-S0 body)                 ║ │
  │   ║ 5. on MISS: synchronous backend UPSERT (durable immediately)         ║ │
  │   ║ 6. record metrics: hit/miss/total (tagged by mode)  [S5]            ║ │
  │   ║ 7. return  (cached vec only when mode==on & hit; else live)          ║ │
  │   ╚════════════════════════════════════════════════════════════════════╝ │
  │                                                                            │
  │   _compute_live()  =  EXACT post-S0 body of coalesced_query_embedding():   │
  │     registry-none / kill-switch / lane-absent / coalesced branches        │
  │     server/services/embedding_coalescer.py                                 │
  │     [S0 FIX] thread embedding_purpose='query' through coalescer.submit()   │
  │       AND set embedding_purpose='query' at every query caller (below)      │
  │     services/voyage_ai.py (no input_type — unaffected)                     │
  │     services/cohere_embedding.py (_map_embedding_purpose — the bug site)   │
  └───────────────────────────────┬───────────────────────────────────────────┘
                                   │
  ┌───────────────────────────────▼───────────────────────────────────────────┐
  │ STORAGE LAYER  (synchronous DB-direct; NO RAM layer, NO async writer)      │
  │  [INSERT S1] backend-dual QueryEmbeddingCacheBackend (BackendRegistry):    │
  │     Protocol:  storage/protocols.py  (mirror ApiMetricsBackend)            │
  │     SQLite  :  storage/sqlite_backends.py        (solo)                    │
  │     Postgres:  storage/postgres/<...>_backend.py (cluster, shared)         │
  │     register:  storage/factory.py — field added to BackendRegistry AND     │
  │       wired in BOTH _create_sqlite_backends() AND _create_postgres_backends│
  │  [INSERT S1] DB migration (CREATE TABLE IF NOT EXISTS only):               │
  │     PG : storage/postgres/migrations/sql/0NN_query_embedding_cache.sql     │
  │     SQLite: backend _ensure_schema() on init                              │
  │  Access is synchronous: SELECT on lookup, UPSERT on miss, last_used touch  │
  │  on hit. Single shared count cap = the true cluster-wide cap.              │
  └────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. New components (inserted)

| component | file (new) | story | role |
|---|---|---|---|
| `QueryEmbeddingCache` service | `server/services/query_embedding_cache.py` | S1 | Orchestrator: mode gate, key build, synchronous backend read/upsert/touch, metric hooks. Wraps `coalesced_query_embedding`. NO RAM layer, NO async writer. |
| Anchor-token key builder | (in the service or `..._key.py`) | S2 | `build_key(text, N)` = first-N tokens in order + sorted tail, CASE PRESERVED. Tokenize via `str.split()` (whitespace runs), strip, drop empties; duplicates kept as a sorted multiset. N=0 sort-all; N>=len exact. |
| `QueryEmbeddingCacheBackend` Protocol | `storage/protocols.py` (add) | S1 | get / upsert / touch_last_used / count / prune_to_max / clear (mirror `ApiMetricsBackend`). |
| SQLite backend impl | `storage/sqlite_backends.py` (add) | S1 | solo storage_mode. |
| Postgres backend impl | `storage/postgres/query_embedding_cache_backend.py` | S1 | cluster storage_mode (shared across nodes). |
| PG migration | `storage/postgres/migrations/sql/0NN_query_embedding_cache.sql` | S1 | `CREATE TABLE IF NOT EXISTS` (rolling-restart safe). |
| `QueryEmbeddingCacheConfig` | server config model (add nested) | S1/S3 | runtime config (NOT BOOTSTRAP_KEYS). |
| OTEL gauges + InMemoryMetricReader test harness | metrics module + tests | S5 | total-entries gauge (cheap periodic/cached count, NOT live COUNT(*) on the exporter thread); hit/miss counters tagged by `mode`; fake-collector assertion. |
| 3b audit hook | search-service layer | S6 | sampled side-by-side HNSW overlap; on-mode sampled hits re-embed. |
| Docs | `docs/query-embedding-cache.md` + empirical writeup | S7 | architecture + operator guide + alternatives analysis. |

### Table schema (S1)
```
query_embedding_cache(
  cache_key   TEXT     NOT NULL,   -- SHA-256 of normalized-query-key (case-preserved)
  provider    TEXT     NOT NULL,   -- 'voyage-ai' | 'cohere'
  model       TEXT     NOT NULL,   -- voyage-code-3 | embed-v4.0
  dimension   INTEGER  NOT NULL,   -- 1024 | 1536
  embedding   BLOB     NOT NULL,   -- float32 little-endian (NOT json)
  created_at  REAL     NOT NULL,   -- epoch seconds
  last_used   REAL     NOT NULL,   -- epoch seconds; bumped on hit; eviction order key
  PRIMARY KEY (cache_key, provider, model, dimension)  -- cross-provider isolation
)
CREATE INDEX IF NOT EXISTS idx_qec_last_used ON query_embedding_cache(last_used);
-- ONLY query-purpose embeddings live here; NEVER write document-purpose vectors.
-- shared count cap (default 10000, min 100) = the single true cluster-wide cap.
-- eviction (lazy prune), deterministic order to break last_used ties:
--   ORDER BY last_used ASC, created_at ASC, cache_key ASC, provider ASC, model ASC, dimension ASC
--   PG    : DELETE ... WHERE ctid  IN (SELECT ctid  ... <ORDER BY> OFFSET :max_entries)
--   SQLite: DELETE ... WHERE rowid IN (SELECT rowid ... <ORDER BY> LIMIT -1 OFFSET :max_entries)
-- NO repo/collection column (embedding is repo-independent).
```

---

## 3. Existing components modified (injection points)

| file | injection | story |
|---|---|---|
| `server/services/governed_call.py` | wrap `coalesced_query_embedding` outside-in; existing body -> `_compute_live()` (the EXACT post-S0 body) | S1 |
| `server/services/embedding_coalescer.py` | thread `embedding_purpose` through `submit()` -> `do_call()` -> `get_embeddings_batch(..., embedding_purpose=...)` | S0 |
| `server/services/search_service.py` (~494) | currently passes `embedding_purpose=None` -> set `"query"`; thread bypass flag | S0, S4 |
| `services/temporal/temporal_search_service.py` (~438) | currently passes `embedding_purpose=None` -> set `"query"`; thread bypass flag | S0, S4 |
| `server/mcp/handlers/search.py` (memory embed ~514) | set `embedding_purpose="query"`; thread bypass flag | S0, S4 |
| `storage/filesystem_vector_store.py` (~2498-2512) | thread bypass flag; host 3b audit at this layer | S4, S6 |
| `server/models/api_models.py` (`SemanticSearchRequest`) | add `no_embedding_cache_shortcut: bool = False` (keyword default) | S4 |
| `server/mcp/tool_docs/search/search_code.md` (+ peers) + `tools/verify_tool_docs.py` | document the new param; pass CI gate | S4 |
| server config model + `get_config_service()` | add `QueryEmbeddingCacheConfig` (runtime, live-reload) | S1, S3 |
| `storage/factory.py` (BackendRegistry) | add field; wire in BOTH `_create_sqlite_backends()` and `_create_postgres_backends()` | S1 |
| `server/startup/lifespan.py` (beside coalescer registry) | build + wire the cache (backend, both storage modes); clear on shutdown; wiring regression guard | S1 |
| Web UI Config screen | new dedicated "Query Embedding Cache" section (2 global + 2x3 per-provider knobs + live metric readout) | S3 |
| dashboard + OTEL setup | register gauges/counters in existing namespace | S5, S6 |
| `CLAUDE.md` | new architecture-invariant section | S7 |

---

## 4. Control-flow by mode (at the wrap)

| mode | lookup | provider call | returned | DB write | metrics |
|---|---|---|---|---|---|
| off | no | always (live) | live | none | none |
| shadow | yes (sync SELECT) | **always** (live) | **live** | sync UPSERT on miss; sync `last_used` touch on hit | hit/miss/total (tagged `mode=shadow`) + 3a cos(cached,live); 3b if sampled (live vec already present) |
| on | yes (sync SELECT) | only on MISS | cached on hit / live on miss | sync UPSERT on miss; sync `last_used` touch on hit | hit/miss/total (tagged `mode=on`); 3b if sampled (sampled hit re-embeds for comparison) |

`no_embedding_cache_shortcut=true`: skip the lookup (force live) but STILL write
(when provider mode != off). Applies in shadow and on.

Hit-ratio is labeled per mode: shadow = "would-serve rate"; on = "serving rate".
Never present a single blended ratio.

---

## 5. Story -> component matrix

| story | primary components |
|---|---|
| **S0** | `embedding_coalescer.py` purpose threading + `search_service.py:494` + `temporal_search_service.py:438` + MCP memory site (all -> `embedding_purpose="query"`); all-sites `search_query` test; isolated commit; bug doc |
| **S1** | `query_embedding_cache.py` (service + outside-in wrap, synchronous), backend Protocol+SQLite+PG, migration, config model, `governed_call.py` `_compute_live()` (post-S0), BackendRegistry double-touch, `lifespan.py` wiring + guard, regression matrix + failure-mode ACs |
| **S2** | key builder (anchor dial, pinned tokenizer) + per-provider `anchor_tokens` config + namespace-change log |
| **S3** | lazy prune (exact per-backend SQL + tie-break) + count-cap (`max_entries`>=100) + Web UI config section + knobs UI |
| **S4** | `api_models.py`, tool docs + verify gate, all 4 query caller layers, keyword-default bypass flag, per-front-door E2E |
| **S5** | OTEL gauges/counters (periodic count, mode-tagged), dashboard, 3a cosine, InMemoryMetricReader harness, shadow-error AC |
| **S6** | 3b audit at search-service layer; on-mode sampled re-embed; per-provider `audit_sample_rate`; audit metrics |
| **S7** | `docs/query-embedding-cache.md`, empirical/alternatives writeup, CLAUDE.md invariant |

---

## 6. Hard invariants (carry into the epic)

- NEVER lowercase the key (CamelCase identifier signal loss — empirically proven).
- Key has NO repo/collection; row qualified by provider+model+dimension (PK).
- Store access is SYNCHRONOUS DB-direct: no RAM layer, no async/batched writer
  (deferred; a RAM layer is a clean additive optimization if QPS ever grows).
- The shared count cap is the SINGLE, TRUE cluster-wide cap.
- The table stores ONLY query-purpose embeddings: NEVER write document-purpose
  vectors (different Cohere semantics).
- Cache value is ONLY query-string -> vector: never auth-bearing data.
- Migration is additive only (CREATE TABLE IF NOT EXISTS) — rolling-restart safe.
- Both backends (SQLite solo + PG cluster) are first-class and E2E-tested.
- S0 fix is an isolated commit and is a prerequisite for S1 storing Cohere vectors.
- `_compute_live()` is the EXACT post-S0 body of `coalesced_query_embedding()`.
