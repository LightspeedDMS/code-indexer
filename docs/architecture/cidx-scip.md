# cidx-scip: SCIP as a feature distribution

Status: phase 4d of the modular packaging plan
([modular-endstate.md](../../plans/designs/modular-endstate.md)). SCIP is a
removable add-on, built exactly like X-Ray ([cidx-xray.md](cidx-xray.md)): the
core imports no `cidx_scip` module and wires nothing of SCIP itself; it
consumes the feature's hooks. The feature contract is in
[feature-sdk.md](feature-sdk.md).

## Turning it on and off

| What | Rule |
|---|---|
| Server side (MCP tools, REST routes, the query page's `scip` mode, the `scip` index type, migration, config section, startup wiring, health) | installed AND `"scip"` in the bootstrap key `enabled_features` (`config.json`) |
| `cidx scip` CLI group | installed (found by entry point), whatever the server's `enabled_features` says |
| Enabled but not installed | startup refused (`FeatureNotInstalledError`) |
| Installed, not enabled | no SCIP tools, routes or search mode; refreshes skip the SCIP step; an add-index request for `scip` is refused as an unknown type; `GET /health` lists `scip` under `installed_not_enabled`; `cidx scip` still works |
| Not installed | no SCIP anywhere; the core starts and runs unchanged |

The core depends on `cidx-scip` by path (`pyproject.toml`), so the default
install and the image carry it. Example `config.json` (bootstrap keys only):

```json
{
  "enabled_features": ["xray", "scip"],
  "feature_bootstrap_settings": {"scip": {"scip_backend": "delegated-plugin"}}
}
```

## What exists

| Piece | Where |
|---|---|
| Distribution `cidx-scip` (import name `cidx_scip`, version 0.1.0) | `packages/features/cidx-scip/` |
| Engine: discovery, generator, indexers, SCIP database, queries, status | `cidx_scip/*` (was `code_indexer/scip/`) |
| `cidx scip` CLI group, its remote API client | `cidx_scip/cli.py`, `cidx_scip/api_client.py` |
| Feature object `scip` | `cidx_scip/feature.py`, entry point `scip = "cidx_scip.feature:make_feature"` |
| Config section, query limits | `cidx_scip/config.py`, `cidx_scip/limits.py` |
| MCP handlers and their tool docs | `cidx_scip/server/mcp/`, `cidx_scip/server/tool_docs/*.md` |
| REST routes | `server/routes.py` (`GET /scip/*`), `server/multi_routes.py` (`POST /api/scip/multi/*`), `server/admin_routes.py` (`/api/admin/scip-cleanup-*`) |
| Query page search mode | `server/web/query_page.py` |
| Index builder (the pipeline's `scip` type) | `server/index_builder.py` |
| Build funnel, delegated build queue, build status, repository reads | `server/lifecycle.py`, `resolution_queue.py`, `build_status.py`, `repo_reads.py` |
| Build backend wiring and plugin resolver | `server/backend_wiring.py`, `server/backend_plugin.py` |
| Query services | `server/query_service.py`, `server/multi_service.py`, `server/multi_models.py` |
| Self-healing, workspace cleanup, dependency audit | `server/self_healing.py`, `server/workspace_cleanup.py`, `server/audit*.py` |
| EVO-65484 backfill | `server/reconcile.py`, `server/backfill_policy.py` |
| Migrations | `server/migrations.py` |
| `on_startup` / `on_shutdown` | `server/wiring.py`, `server/runtime.py` |
| The one core adapter; thin server calls | `cidx_scip/_platform.py`; `server/_core.py` |
| Feature tests / core integration tests | `packages/features/cidx-scip/tests/` / the SCIP files under `tests/unit/` |

Stayed in the core: the `scip` tool category (`tool_docs/scip/_category.yaml`,
empty without the feature), the registry flag `enable_scip` and the
filesystem index status of `get_index_status`, the `scip_multi_*` fan-out
limits (`multi_search_limits_config`, shared with multi-search), the
search-event recorders SCIP doors call (`record_scip_search`,
`in_scip_search_scope`'s scope, `scip_payload_outcome`), the removed
`scip_query_backend` guard (`server/startup/removed_scip_settings.py`, runs
feature or not), and the PR / git-cleanup audit history routes (they read the
core's audit log). `code_indexer/scip/delegation_api.py` stays as the plugin
API's old import path (SDK names plus the core's sandbox-mcp client; it
imports no feature code).

## How each hook is consumed

| Hook | Contribution | Core consumer |
|---|---|---|
| `manifest` | `requires_sdk` `>=0.7,<0.8`, optional seam `scip`, capabilities config, storage, permissions, background_jobs | loader |
| `tools()` | the eleven `scip_*` tools, docs read from the wheel | `features/tools.py` |
| `routers()` | `/scip/*`, `/api/scip/multi/*` (any authenticated caller), `/api/admin/scip-cleanup-*` (`manage_users`) | `features/web.py` |
| `web_query_modes()` | the query page's `scip` mode | `features/query_modes.py`, `web/pages/query.py` |
| `index_builders()` | the `scip` index type | `features/index_builders.py` -> `kernel/index_builders.py` |
| `cli_commands()` | `cidx scip` | `cli_features.py` |
| `config_section()` | runtime `scip_reference_limit`, `scip_dependency_depth`, `scip_callchain_max_depth`, `scip_callchain_limit`, `scip_workspace_retention_days` (bounded); bootstrap `scip_backend` | `features/config.py`; shown and edited only in the generic Feature Settings block ("SCIP Code Intelligence") |
| `migrations()` | 1: `scip_dependency_installations` | `features/migrations.py` |
| `background_jobs()` | `scip_reconcile`, only while `CIDX_SCIP_AUTO_RECONCILE_ENABLED=true` | `features/jobs.py` (one node per tick) |
| `on_startup` / `on_shutdown` | `scip_backend` validation, audit repository, workspace cleanup; stops the build queue | `features/registry.py` |
| `health()` | `scip-backend`: the delegated plugin is resolvable | health block |

## The indexing pipeline: how SCIP gets built

The core keeps the pipeline (which repositories carry `enable_scip`, refresh
scheduling, progress, the snapshot that publishes an index); the feature
builds. SDK 0.5's `IndexBuilderContribution` is the extension point: the
core looks the `scip` type up in `kernel/index_builders.py` and asks it.

| Entry point | Feature enabled | Feature absent |
|---|---|---|
| Golden refresh (`refresh_scheduler._run_source_scip`) | `build()`: `run_locally` -> the core runs `cidx scip generate` (cancellable, no timeout); delegated -> enqueued, phase ends `SCIP: enqueued` | the step is skipped and logged; `enable_scip` is left as stored |
| Add-index (`golden_repo_manager._run_feature_index`) | same; `skipped` fails the job (EVO-64616); a delegated build answers "enqueued, runs asynchronously" | `scip` is not a valid index type |
| Activated reindex (`activated_repo_index_manager._execute_scip_indexing`) | always local: `cidx scip generate --project <path> [--clear]` | skipped, reported as success |
| `get_index_status` | `annotate_status` adds the delegated build state | the filesystem status only |
| Registry flag reconcile | `index_present` | flag untouched |

## Capability use

| Need | Through |
|---|---|
| query limits, retention, `scip_backend` | `config` capability (`runtime.config_section()`) |
| audit table (SQLite solo, PostgreSQL cluster) | `storage` capability |
| REST caller's permissions | `permissions` capability (`route_auth.RouteCaller`) |
| search-event recording, query admission, repository access, the job tracker, served-index reads, the content and git seams for mount-less reads, the golden repo manager, reranking, the elevation gate | `_platform.server_platform()` (`ServerPlatformHost`; methods added in 4d are listed under "services (SCIP, phase 4d)") |

## Configuration

`ServerConfig.scip_backend` is gone, and `ScipConfig` keeps only the core's
`temporal_stale_threshold_days`. On load,
`config_manager._migrate_feature_owned_keys` moves a top-level `scip_backend`
into `feature_bootstrap_settings["scip"]` and the SCIP keys of a stored
`scip_config` (and a pre-Story #15 top-level `scip_workspace_retention_days`)
into `feature_settings["scip"]`; a value already in the section wins.
`CIDX_SCIP_WORKSPACE_RETENTION_DAYS` still overrides the retention. The
runtime keys are `restart_required` (read once at startup) and bounded by
`cidx_scip.limits`, the only copy of those bounds. The core's config screen
keeps `temporal_stale_threshold_days` in its own "Temporal Index Staleness"
section (still posted as section `scip`, stored as `scip_config`). The
golden-mount check counts
`scip.scip_backend` like any feature selection: anything but `local` is
mount-free.

## Migration 1 and the former audit storage

| Database | What version 1 does |
|---|---|
| PostgreSQL, core 025 applied | no DDL; version 1 recorded (the core keeps shipping 025) |
| PostgreSQL, 025 not applied | creates the table and indexes |
| SQLite (solo) | creates the table in the server database, then copies the rows of `<server dir>/scip_audit.db` when it exists (left in place) |

`BackendRegistry.scip_audit`, `startup/database_init.py` and the
`scip_audit.db` health entry are gone; `migrate_to_postgres --scip-audit-path`
still reads an old file.

## Import contracts

| Contract | Baseline |
|---|---|
| `core-does-not-import-scip-feature` | 0 |
| `scip-feature-no-core` | 12, all in `cidx_scip._platform` |
| `features-not-imported` (now: nothing imports `code_indexer.scip`) | 0 (was 74 lines) |

## Tests

```bash
PYTHONPATH=<src and every packages/*/src> pytest packages/features/cidx-scip/tests \
  tests/unit/server/features/test_scip_removability.py tests/unit/scip tests/unit/test_scip_*.py
```

Tests that drive SCIP through the MCP dispatcher or a FastAPI app install it
with `tests/unit/server/_scip_feature.py`. `test_scip_removability.py` proves
the three states (enabled, installed but disabled, uninstalled).
