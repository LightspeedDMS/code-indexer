# cidx-xray: X-Ray as a feature distribution

Status: phase 4c of the modular packaging plan
([modular-endstate.md](../../plans/designs/modular-endstate.md),
[modular-packaging-addons.md](../../plans/designs/modular-packaging-addons.md)).
X-Ray is a removable add-on: the core imports no `cidx_xray` module and wires
nothing of X-Ray itself; it consumes the feature's hooks. This page describes
the code as built. The feature contract is in [feature-sdk.md](feature-sdk.md).

## Turning it on and off

| What | Rule |
|---|---|
| Server side (MCP tools, REST routes, migration, config section, startup wiring, health) | installed AND `"xray"` in the bootstrap key `enabled_features` (`config.json`) |
| `cidx xray` CLI group | installed (found by entry point), whatever the server's `enabled_features` says |
| Enabled but not installed | startup refused (`FeatureNotInstalledError`) |
| Installed, not enabled | the server has no X-Ray tools, routes or table; `GET /health` lists `xray` under `installed_not_enabled`; `cidx xray` still works |
| Not installed | no X-Ray anywhere; the core starts and runs unchanged |

The core still depends on `cidx-xray` by path (`pyproject.toml`), so the default
install and the image carry it; enabling it on a server is the operator's
choice. Example `config.json` (bootstrap keys only):

```json
{
  "enabled_features": ["xray"],
  "feature_bootstrap_settings": {"xray": {"xray_backend": "delegated"}}
}
```

## What exists

| Piece | Where |
|---|---|
| Distribution `cidx-xray` (import name `cidx_xray`, version 0.2.0) | `packages/features/cidx-xray/` |
| Engine: AST engine, sandbox, Rust backend, search engine, execution seam, inline execution, evaluator cache store | `cidx_xray/*.py` |
| `cidx xray` CLI group | `cidx_xray/cli.py` |
| Feature object `xray` | `cidx_xray/feature.py`, entry point `xray = "cidx_xray.feature:make_feature"` (group `code_indexer.features`) |
| Config section | `cidx_xray/config.py` |
| MCP handlers and their tool docs | `cidx_xray/server/mcp/{xray,xray_batch,xray_graph}`, `cidx_xray/server/tool_docs/*.md` |
| REST routes | `cidx_xray/server/routes.py` |
| Pattern library service (parameters, seeds), graph governor | `cidx_xray/server/pattern_service.py` (`pattern_params.py`, `pattern_seeds.py`), `cidx_xray/server/graph_governor/` |
| Execution placement and the delegated plugin resolver | `cidx_xray/server/backend_wiring.py`, `cidx_xray/server/backend_plugin.py` |
| Migrations | `cidx_xray/server/migrations.py` (SQL in `graph_governor/k_calibration_samples.sql`) |
| `on_startup` / `on_shutdown` and what they install | `cidx_xray/server/lifecycle.py`, `cidx_xray/server/runtime.py` |
| The one core adapter | `cidx_xray/_platform.py` |
| Test helpers shared by both test trees | `cidx_xray/testing.py` |
| Engine tests / core integration tests | `packages/features/cidx-xray/tests/` / `tests/unit/xray/` and the X-Ray files under `tests/unit/server/` |

Moved in phase 4c (old core path -> new):

| Old | New |
|---|---|
| `server/mcp/handlers/xray/` (`_infra`, `_search`, `_explore`, `_dump_ast`, `_store_pattern`) | `cidx_xray/server/mcp/xray/` |
| `server/mcp/handlers/xray_batch.py`, `xray_graph/` | `cidx_xray/server/mcp/xray_batch.py`, `xray_graph/` |
| `server/mcp/tool_docs/search/{xray_search,xray_explore,xray_dump_ast,xray_search_batch,store_xray_pattern,analyze_graph}.md` | `cidx_xray/server/tool_docs/` |
| `server/routes/xray_routes.py` | `cidx_xray/server/routes.py` |
| `server/services/xray_pattern_service.py` | `cidx_xray/server/pattern_service.py` |
| `server/services/xray_graph_governor/` (minus the composite) | `cidx_xray/server/graph_governor/` |
| `server/storage/postgres/xray_graph_k_calibration_backend.py` | `cidx_xray/server/graph_governor/k_calibration_postgres.py` |
| `server/startup/xray_backend_wiring.py`, `server/services/xray_backend_plugin.py` | `cidx_xray/server/backend_wiring.py`, `backend_plugin.py` |
| PG migration `051_xray_graph_k_calibration_samples.sql` | feature migration 1 |
| X-Ray wiring in `startup/lifespan.py` and `startup/service_init.py` | `cidx_xray/server/lifecycle.py` |
| `code_indexer/xray/` (alias package), `code_indexer/cli_xray.py`, `server/storage/postgres/xray_cache_backend.py` (shims) | removed: nothing in the core imports them; tests import `cidx_xray` |

Stayed in the core, because they serve every tool: `cidx_fetch_cached_payload`
(`handlers/cached_payload.py`), `cancel_job` (`handlers/cancel_job.py`), the
paged payload-cache protocol (`handlers/xray_truncation*.py`), the governor's
composite cache (`services/governed_cache_composite.py`).

## How each hook is consumed

| Hook | Contribution | Core consumer |
|---|---|---|
| `manifest` | `requires_sdk` `>=0.7,<0.8`, optional seam `xray`, required capabilities `CAPABILITIES_FOR_HANDLERS` (config, access_filter, permissions, background_jobs, payload_cache, search_events, telemetry, storage) | loader |
| `tools()` | the six tools above, docs read from the wheel; `xray_search`, `xray_explore` and `analyze_graph` declare `manages_own_timeout` (SDK 0.5) | `features/tools.py` (TOOL_REGISTRY, HANDLER_REGISTRY, doc loader); `features/dispatch_policy.py` (no generic MCP dispatch timeout) |
| `job_types()` | `xray_search`, `xray_explore`, `xray_search_batch` hidden from the dashboard; batches carry no `repo_alias` (SDK 0.5) | `features/dispatch_policy.py` (dashboard, job manager) |
| `routers()` | `POST /api/xray/search`, `POST /api/xray/search/batch`, prefix `/api/xray`, required permission `query_repos` | `features/web.py` (core authentication; a caller without `query_repos` gets the core's 403) |
| `cli_commands()` | the `cidx xray` group | `cli_features.py`, every installed feature |
| `config_section()` | runtime `xray_timeout_seconds` (120), `xray_worker_threads` (4); bootstrap `xray_backend` (`local`) | `features/config.py`; Feature Settings block and admin API |
| `migrations()` | 1: `xray_graph_k_calibration_samples` | `features/migrations.py` (row in `feature_schema_versions`) |
| `on_startup` / `on_shutdown` | executor, execution backend + placement host, cell limiter (resized on a committed `xray_worker_threads` change), graph cache under the memory governor, K provider | `features/registry.py` |
| `health()` | `xray-cli` present, or execution delegated | health block |

### What the handlers reach, and how

| Need | Through |
|---|---|
| timeout and worker defaults | `config` capability (`runtime.config_section()`) |
| timeout-config failure counter | `telemetry` capability |
| batch inline-result polling | `background_jobs` capability (`status`) |
| REST caller's other permissions | `permissions` capability (`routes.RouteCaller`) |
| K-calibration store (SQLite solo, PostgreSQL cluster) | `storage` capability |
| job tracker lifecycle, the job manager's child-process kill, paged payload-cache writes, the X-Ray search-event recorder (`run_recorded_xray`), the group-based repository guard, repository resolution and bare-alias fallback, query admission, content-seam manifest reads, cidx-meta writes and refresh requests, the pattern-store scope grant and admin role, the audit outcome rows, the catalog access projection's X-Ray refusal (`golden_catalog_surface_refusal`, policy in `cidx_meta_access/history_policy.py`) | `_platform.server_platform()`: the kernel slot `SERVER_PLATFORM_HOST` (`kernel/capabilities.ServerPlatformHost`), filled by the core's `server/feature_platform.py` |
| content seam, regex target, placement, runtime config, telemetry for the engine | `_platform.capabilities` (kernel slots) |
| process-group termination of `xray-cli` (compile reaping), the time-budgeted Phase 1 path-driver regex (`BoundedPattern`) | `_platform` re-exports of the core's pure helpers `utils/process_group.py`, `utils/bounded_regex.py` |

The SDK Protocols of `payload_cache`, `search_events`, `access_filter` and
`background_jobs` are narrower than what the handlers use (paged writes, the
outcome-classifying recorder, the admin-group guard, the tracker lifecycle), so
those go through `ServerPlatformHost`. Each of its method groups is a
candidate SDK capability; when one exists the methods leave the Protocol.
`ServerPlatformHost` names no feature: any add-on may use it.

## Configuration

`ServerConfig.xray_config` and `ServerConfig.xray_backend` are gone. On load,
`config_manager._migrate_feature_owned_keys` moves a stored `xray_config`
(runtime database row) into `feature_settings["xray"]` and a top-level
`xray_backend` (`config.json`) into `feature_bootstrap_settings["xray"]`; a
value already in the section wins. Nothing is lost on upgrade, and a
`config.json` still carrying `"xray_backend"` keeps working.

The config screen shows and edits the keys only in the generic Feature
Settings block (section title "X-Ray Search", the timeout bounded 10..600, the
worker threads 1..8, both declared on the `ConfigKey`s); the core has no X-Ray
section, category or validation of its own.

The golden-mount check (`startup/golden_mount_guard.py`) requires every
`*_backend` bootstrap setting of an enabled feature to be `delegated` for a
mount-free pod; X-Ray disabled requires nothing.

## Migration 1 and the former core 051

| Database | What version 1 does |
|---|---|
| PostgreSQL, core 051 already applied (`schema_migrations` has it) | no DDL; the runner records version 1 (imports the state) |
| PostgreSQL, fresh | creates the table and index (the 051 SQL) |
| SQLite (solo) | creates the SQLite form in the server database |

Removing the feature leaves the table. In solo mode the samples used to live
in a dedicated `xray_k_calibration.db`; they are not carried over (nothing
reads K yet, and K falls back to the conservative seed).

## Import contracts

| Contract | Baseline after 4c |
|---|---|
| `core-does-not-import-xray-feature` | 0 (was 26) |
| `xray-feature-no-core` | 5, all in `cidx_xray._platform` (unchanged); 7 after the v12.93.0 snapshot (`utils.process_group`, `utils.bounded_regex`: upstream's compile reaping and bounded path driver; `bounded_regex` needs the `regex` package, which the SDK cannot depend on) |

`ServerPlatformHost` is reached through the already-imported
`code_indexer.kernel.capabilities`, so moving the server modules added no
import. The content-seam inventory and the git-seam guard seed the in-tree
feature packages directly, since the core's import closure no longer reaches
them.

## The Rust engine (`rust/`)

Decision: `rust/` stays at the repository root and is built where it is used,
not shipped as platform wheels.

| Context | Where `xray-cli` comes from |
|---|---|
| Container image | `xray-builder` stage builds it from `rust/`; `CIDX_XRAY_CLI_PATH=/usr/local/bin/xray-cli` |
| Source checkout | `<root>/rust/target/release/xray-cli`, found by walking up from `cidx_xray/rust_backend.py` |
| Any other install | `CIDX_XRAY_CLI_PATH` |

## Tests

```bash
PYTHONPATH=src:packages/cidx-sdk/src:packages/features/cidx-xray/src:packages/backends/cidx-backend-workspace/src:packages/cidx-testkit/src \
  pytest tests/unit/xray packages/features/cidx-xray/tests tests/unit/server/features/test_xray_removability.py
```

A source-tree run finds the feature through `cidx_testkit.registry` (the
entry points the in-tree distributions declare). Tests that drive X-Ray through
the MCP dispatcher or a FastAPI app install it with
`tests/unit/server/_xray_feature.py`. `test_xray_removability.py` proves the
three states (enabled, installed but disabled, uninstalled);
`test_xray_feature_lifecycle.py` the startup wiring;
`test_xray_feature_migrations.py` the 051 import.
