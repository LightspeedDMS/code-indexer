# cidx-memory: shared technical memories as a feature distribution

Status: phase 4f of the modular packaging plan
([modular-endstate.md](../../plans/designs/modular-endstate.md)). The memory
store is a removable add-on built the way X-Ray and the wiki are
([cidx-xray.md](cidx-xray.md), [cidx-wiki.md](cidx-wiki.md)): the core imports
no `cidx_memory` module and wires nothing of the store itself; it consumes the
feature's hooks. The feature contract is in [feature-sdk.md](feature-sdk.md);
how retrieval behaves is in [docs/server/memory-retrieval.md](../server/memory-retrieval.md).

## Turning it on and off

| What | Rule |
|---|---|
| Server side (MCP `create_memory` / `edit_memory` / `delete_memory`, REST `/api/v1/memories`, the `relevant_memories` section of `search_code`, config section, the memory store, health) | installed AND `"memory"` in the bootstrap key `enabled_features` (`config.json`) |
| Enabled but not installed | startup refused (`FeatureNotInstalledError`) |
| Installed, not enabled | no memory tools, routes or search section; `GET /health` lists `memory` under `installed_not_enabled` |
| Not installed | no memory store anywhere; the core starts and runs unchanged |

The memory feature has no CLI commands and no tables. The core depends on
`cidx-memory` by path (`pyproject.toml`), so the default install and the image
carry it. Example `config.json`:

```json
{"enabled_features": ["xray", "wiki", "memory"]}
```

Data is kept whichever state: memories are files in the catalog
(`cidx-meta/memories/<id>.md`), indexed with the catalog and served by its file
doors, filtered per caller, with or without the feature.

## What exists

| Piece | Where |
|---|---|
| Distribution `cidx-memory` (import name `cidx_memory`, version 0.1.0) | `packages/features/cidx-memory/` |
| Feature object `memory` | `cidx_memory/feature.py`, entry point `memory = "cidx_memory.feature:make_feature"` |
| Config section | `cidx_memory/config.py` (`MemoryRetrievalConfig` lives here now) |
| MCP handlers and tool docs | `cidx_memory/server/mcp/handlers.py`, `cidx_memory/server/tool_docs/` |
| REST routes | `cidx_memory/server/routes.py` |
| Store, locks, rate limiter, schema | `cidx_memory/server/memory_{store_service,store_service_factory,file_lock_manager,rate_limiter,schema}.py` |
| Search section (retrieval) | `cidx_memory/server/{retrieval,memory_retrieval_pipeline,memory_candidate_retriever}.py`, `prompts/memory_empty_nudge.md` |
| `on_startup` / `on_shutdown` | `cidx_memory/server/lifecycle.py`, `runtime.py` |
| The one core adapter | `cidx_memory/_platform.py` |
| Tests | `packages/features/cidx-memory/tests/`; core integration tests stay in `tests/unit/server/{services,mcp,routers}/` (they import `cidx_memory`); helper `tests/unit/server/_memory_feature.py`; removability: `tests/unit/server/features/test_memory_removability.py` |

Moved (old core path -> new):

| Old | New |
|---|---|
| `server/services/memory_{store_service,store_service_factory,file_lock_manager,rate_limiter,schema,candidate_retriever}.py` | `cidx_memory/server/` |
| `server/mcp/memory_retrieval_pipeline.py`, `server/mcp/prompts/memory_empty_nudge.md` | `cidx_memory/server/` |
| `server/mcp/handlers/memory.py` | `cidx_memory/server/mcp/handlers.py` |
| `server/routers/memories.py` | `cidx_memory/server/routes.py` |
| `server/mcp/tool_docs/memory/*.md` | `cidx_memory/server/tool_docs/` |
| `_run_memory_retrieval`, `_visible_candidates`, `_compute_memory_query_vector` in `server/mcp/handlers/search/memory_retrieval.py` | `cidx_memory/server/retrieval.py` (no own embedding call: the section takes the core's vector) |
| `ServerConfig.memory_retrieval_config` / `MemoryRetrievalConfig` | the `memory` config section / `cidx_memory.config.MemoryRetrievalConfig` |
| Store construction in `startup/lifespan.py`, `app.state.memory_store_service`, `AppState.memory_store_service` | `on_startup` (`runtime.STORE`) |

### Stayed in the core, and why

| Piece | Why |
|---|---|
| `server/services/memory_io.py` (memory file format: front matter, atomic write/delete, read) | The core's catalog access projection reads memory front matter to decide who may see a memory file (`AccessFilteringService`, `cidx_meta_access.memory_visibility_fn`); the files exist and are served whether or not the feature is installed. The feature writes through the same module (`_platform`) |
| `AccessFilteringService` memory rule (`filter_visible_memory_ids`, `memory_visible_within`, `memories_dir`), `cidx_meta_access` path policy (`MEMORIES_DIR`) | It is the security projection, applied above the seam once (end state, section 7); the feature reaches it as `access_projection.visible_memory_ids` |
| `memories_dir` wiring of `AccessFilteringService` and the NFS atomic-create self-check in the lifespan | Same reason: the catalog doors need it with the feature off |
| `search/query_vector.py` (`_compute_shared_query_vector`) | The query embedding is core query work, shared by the omni fan-out and every search section |
| `server/mcp/tool_docs/memory/_category.yaml` | The tool-doc loader's category list is core (SCIP's category stays the same way) |
| `server/services/memory_governor.py` | Not this feature: the index-cache memory-pressure governor |
| `app.state.server_data_dir` (new, set in the lifespan) | Generic platform data: where server-wide lock files live |

## How each hook is consumed

| Hook | Contribution | Core consumer |
|---|---|---|
| `manifest` | `requires_sdk` `>=0.7,<0.8` (SDK 0.7 adds `search_augmenters()` and `AccessProjectionCapability.visible_memory_ids`); capabilities `config`, `access_projection` | loader |
| `tools()` | `create_memory`, `edit_memory`, `delete_memory` (`repository:write`), docs from the wheel | `features/tools.py` |
| `routers()` | `/api/v1/memories` (`auth="api"`): the core authenticates; routes read `request.state.cidx_principal` | `features/web.py` |
| `search_augmenters()` | `relevant_memories` for `semantic` / `hybrid`, active while `memory_retrieval_enabled` | `features/search_augmenters.py`, called by `mcp/handlers/search/repo_search.py` |
| `config_section()` | runtime `memory_retrieval_enabled` (`true`), `memory_voyage_min_score` (`0.5`, 0..1), `memory_cohere_min_score` (`0.4`, 0..1), `memory_retrieval_k_multiplier` (`5`, >=1), `memory_retrieval_max_body_chars` (`2000`, >=1) | `features/config.py`; shown and edited only in the generic Feature Settings block ("Memory Retrieval") |
| `on_startup` / `on_shutdown` | context; the memory store over `golden_repos_dir/cidx-meta/memories`, `server_data_dir/locks`, the refresh scheduler and the cidx-meta debouncer (not built without them: tools answer `service_unavailable`, routes 503) | `features/registry.py` |
| `health()` | `memory-assets`: the empty-state nudge in the wheel; `store_running` | health block |

### What the memory feature reaches, and how

| Need | Through |
|---|---|
| retrieval settings | `config` capability (`runtime.memory_retrieval_config()`) |
| which memories a caller may see | `access_projection.visible_memory_ids` (`retrieval.visible_candidates`) |
| the memory file format, catalog writes to a shared catalog, the catalog refresh request, the `memories` vector collection | `_platform` (`memory_io`, `cidx_meta_content`, `request_cidx_meta_refresh`, `FilesystemVectorStore`) |
| app state (golden repositories directory, server data directory, debouncer), the refresh scheduler, the MCP envelope, the error-log format | `_platform.server_platform()` (`ServerPlatformHost`, transitional) |

No other feature uses the memory store; X-Ray's pattern service only mirrors
its cidx-meta lock discipline in comments.

## Catalog access projection

The memory section never decides visibility: it asks
`access_projection.visible_memory_ids(viewer, ids)` once per search, before
any memory body is read. The core binds it to
`AccessFilteringService.filter_visible_memory_ids` (admins see everything;
`scope: global` is visible; `repo`/`file` only when `referenced_repo` is
granted; anything else, and any caller when there is no access service, sees
nothing). `tests/unit/ci/test_catalog_guard_presence.py` requires both ends:
the core capability calls `filter_visible_memory_ids`, and
`retrieval.visible_candidates` / `relevant_memories` call it. Known gaps stay `[]`.

## Configuration

`ServerConfig.memory_retrieval_config` is gone. On load,
`config_manager._migrate_feature_owned_keys` moves a stored
`memory_retrieval_config` into `feature_settings["memory"]`; a value already in
the section wins. The store's limits (summary length, rate limit, lock TTLs)
were never settings and are not now; there are no bootstrap keys.

## Behaviour changes

- Without the feature, `search_code` answers no `relevant_memories` and computes
  no query embedding for it.
- If the core's shared embedding call fails, no section is added; before the
  move `_run_memory_retrieval` made a second embedding attempt.
- The retrieval settings now have bounds (see the config table); out-of-range
  values are refused at update instead of failing at search time.

## Import contracts

| Contract | Baseline |
|---|---|
| `core-does-not-import-memory-feature` | 0 (the core referenced the memory modules from 7 places before the move: `handlers/__init__`, `handlers/_legacy`, `routers/inline_routes`, `startup/lifespan`, `search/__init__`, `search/memory_retrieval` x2) |
| `memory-feature-no-core` | 5, all in `cidx_memory._platform` (`kernel.capabilities`, `server.services.memory_io`, `server.services.cidx_meta_content`, `storage.filesystem_vector_store`, `global_repos.meta_description_hook`; the last two `TYPE_CHECKING`, resolved lazily) |
| `memory-feature-no-other-feature`, and the other features' `*-no-other-feature` | 0 |

## Tests

```bash
PYTHONPATH=src:packages/cidx-sdk/src:packages/features/cidx-memory/src:packages/features/cidx-wiki/src:packages/features/cidx-xray/src:packages/features/cidx-scip/src:packages/backends/cidx-backend-workspace/src:packages/backends/cidx-backend-git/src:packages/cidx-testkit/src \
  pytest packages/features/cidx-memory/tests tests/unit/server/features/test_memory_removability.py \
    tests/unit/server/mcp/test_memory_handlers.py tests/unit/server/routers/test_memories_router.py \
    tests/unit/server/mcp/test_search_memory_retrieval.py tests/unit/server/mcp/test_memory_retrieval_repository_visibility.py
```

## Not done yet

- `memory_io` and the memory scope rule stay in the core (see above); an SDK
  utility for the memory file format would let the feature drop that adapter
  import.
- The catalog writes, refresh request and vector collection are reached through
  `_platform`; they become SDK capabilities (catalog writes, index search).
- `tool_docs/memory/_category.yaml` stays in the core until feature tool
  categories are contributed.
