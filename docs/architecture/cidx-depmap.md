# cidx-depmap: the dependency map as a feature distribution

Status: phase 4g of the modular packaging plan
([modular-endstate.md](../../plans/designs/modular-endstate.md)). The
dependency map is a removable add-on built the way X-Ray and the wiki are
([cidx-xray.md](cidx-xray.md), [cidx-wiki.md](cidx-wiki.md)): the core imports
no `cidx_depmap` module and wires nothing of the dependency map itself; it
consumes the feature's hooks. The feature contract is in
[feature-sdk.md](feature-sdk.md).

## Turning it on and off

| What | Rule |
|---|---|
| Server side (MCP `depmap_*` and `trigger_dependency_analysis`, the `/admin/dependency-map` page and its `/admin` routes and partials, config section, migration, the service and its scheduler, the description fact check, health) | installed AND `"depmap"` in the bootstrap key `enabled_features` (`config.json`) |
| Enabled but not installed | startup refused (`FeatureNotInstalledError`) |
| Installed, not enabled | none of it; `GET /health` lists `depmap` under `installed_not_enabled`; the navigation has no Dependency Map link (the feature declares it: `RouterContribution.nav_label`, shown to `manage_users`) and the config screen no dependency-map settings (its Feature Settings block lists loaded features only); repository descriptions are written without the fact check |
| Not installed | no dependency map anywhere; the core starts and runs unchanged |

The dependency map has no CLI commands. The core depends on `cidx-depmap` by
path (`pyproject.toml`), so the default install and the image carry it.
Example `config.json`:

```json
{"enabled_features": ["xray", "wiki", "memory", "depmap"]}
```

Data is kept whichever state: the documents are files in the catalog
(`cidx-meta/dependency-map/`), indexed with the catalog and served by its file
doors, filtered per caller; the dependency-map tables stay, inert.

## What exists

| Piece | Where |
|---|---|
| Distribution `cidx-depmap` (import name `cidx_depmap`, version 0.1.0) | `packages/features/cidx-depmap/` |
| Feature object `depmap` | `cidx_depmap/feature.py`, entry point `depmap = "cidx_depmap.feature:make_feature"` |
| Config section | `cidx_depmap/config.py`; the view the code reads (`DepMapClaudeConfig`, `DepMapConfigManager`) in `server/runtime.py` |
| Service, analyzer, dashboard, domain service, delta journal, health detector, index regenerator, MCP parser and graph, repair executor and repair passes, delegated refusal | `cidx_depmap/server/` (same module names) |
| MCP handlers and tool docs | `server/mcp/handlers.py`, `_depmap_aliases.py`, `trigger.py`; `server/tool_docs/` |
| Page, routes, partials, templates, script | `server/web/routes.py`, `caller_view.py`, `templates/`, `static/dependency_map.js` |
| Migration | `server/migrations.py` |
| `on_startup` / `on_shutdown` | `server/lifecycle.py`, `runtime.py` |
| The one core adapter | `cidx_depmap/_platform.py` |
| Tests | `packages/features/cidx-depmap/tests/`; core integration tests stay in `tests/unit/` (they import `cidx_depmap`); helper `tests/unit/server/_depmap_feature.py` (the autouse `_depmap_feature_context` in `tests/unit/server/conftest.py` gives every server test the started feature's context); removability: `tests/unit/server/features/test_depmap_removability.py` |

Moved (old core path -> new):

| Old | New |
|---|---|
| `server/services/dep_map_{delegated,delta_journal,health_detector,index_regenerator,mcp_parser,parser_graph,repair_*}.py`, `server/services/dependency_map_{service,analyzer,dashboard_service,dashboard_job_runner,domain_service}.py`, `global_repos/dependency_map_analyzer.py` | `cidx_depmap/server/` |
| `server/web/dependency_map_routes.py`, `web/depmap_caller_view.py` | `cidx_depmap/server/web/routes.py`, `caller_view.py` |
| `server/web/templates/dependency_map.html`, `partials/depmap_*.html`, `static/js/dependency_map.js` | `cidx_depmap/server/web/templates/`, `static/` (served at `/admin/dependency-map/_static/`) |
| `server/mcp/handlers/depmap.py`, `_depmap_aliases.py` | `cidx_depmap/server/mcp/handlers.py`, `_depmap_aliases.py` |
| `handle_trigger_dependency_analysis` in `server/mcp/handlers/admin/__init__.py` | `cidx_depmap/server/mcp/trigger.py` |
| `server/mcp/tool_docs/depmap/*.md`, `tool_docs/repos/trigger_dependency_analysis.md` | `cidx_depmap/server/tool_docs/` |
| Service construction, startup cleanups, repair invoker, cancel handlers in `startup/lifespan.py`; scheduler stop at shutdown; `AppState.dependency_map_service` | `server/lifecycle.py`; the running service is kept in the feature's slot `runtime.SERVICE` (no app-state name) |
| `DependencyMapService._get_cidx_meta_read_path` | core `server/services/cidx_meta_read_mirror.catalog_read_path` (the core projection needs it too); the service calls it |
| 12 `ClaudeIntegrationConfig` fields (see Configuration) | the `depmap` config section |

### Stayed in the core, and why

| Piece | Why |
|---|---|
| `server/services/cidx_meta_access/depmap_{model,projection,source}.py`, `universe_provider`, the path policy | The catalog access projection: applied by the core's file doors to every catalog file (with or without the feature), and above the seam once (end state, section 7). The feature reaches it as `access_projection.dependency_map_access` |
| `server/services/dep_map_source.py`, `dep_map_parser_tables.py`, `dep_map_parser_hygiene.py`, `dep_map_file_utils.py` | The dependency-map document format the projection parses (`depmap_model`, `depmap_projection`, `depmap_source` import them); the feature uses the same parsers through `_platform` |
| `server/services/dep_map_dispatcher_factory.py` | The generic Claude/Codex CLI dispatcher: the core's lifecycle invoker and self-monitoring scanner build theirs with it |
| Storage: `DependencyMapTrackingBackend` (SQLite/PostgreSQL), the dashboard cache backend, `BackendRegistry.dependency_map_tracking`, core migrations 001/021, `invalidate_dependency_map_tracking` on golden-repo removal, the data-retention cleanup of the run history (`dep_map_history_retention_hours`), the SQLite-to-PostgreSQL data migration | Storage backends and their Protocols stay in the core (end state, section 9), as the wiki's cache backends did; the feature reaches them through `_platform` |
| `server/mcp/tool_docs/depmap/_category.yaml` | The tool-doc loader's category list is core (as SCIP's) |
| The dependency-map section of `cidx_quick_reference` (`mcp/handlers/guides.py`) | It reads the catalog's `dependency-map/` through the catalog projection, like the file doors |
| Tool names in `mcp/protocol.py` (`_REPO_NAME_PARAM_TOOLS`) and `analysis_agent_principal.ANALYSIS_AGENT_TOOLS` | The repository pre-check and the analysis agent's allowlist name the tools; with the feature off the names match no tool (as for SCIP) |
| Lock-owner name `"dependency_map_service"` (passed by the feature's service; the core's lock files record it) | A value, not a reference; the core's source never names the service (`test_depmap_removability.test_core_never_names_the_dependency_map_service`) |

## How each hook is consumed

| Hook | Contribution | Core consumer |
|---|---|---|
| `manifest` | `requires_sdk` `>=0.7,<0.8` (SDK 0.7 adds `DependencyMapView`, `DependencyMapAccess`, `AccessProjectionCapability.dependency_map_access`); capabilities `config`, `access_projection` | loader |
| `tools()` | `depmap_find_consumers`, `depmap_get_cross_domain_graph`, `depmap_get_domain_summary`, `depmap_get_hub_domains`, `depmap_get_repo_domains`, `depmap_get_stale_domains`, `trigger_dependency_analysis` | `features/tools.py` |
| `routers()` | 1. `/admin/dependency-map` (the page; `auth="hybrid"`, `login_redirect`, `static_dir`); 2. `/admin` (actions, data, partials; `auth="hybrid"`, 401). Each route still checks its own Web UI session and admin role as before | `features/web.py` |
| `config_section()` | the 12 runtime keys | `features/config.py`; shown and edited only in the generic Feature Settings block ("Dependency Map"), the core's Claude CLI section holds core keys only |
| `migrations()` | 1: adopts the core's tables, no DDL | `features/migrations.py` |
| `on_startup` / `on_shutdown` | context; the service (tracking backend, analyzer, cluster dedup pool, repair invoker, scheduler, cancel handlers) in the feature's slot `runtime.SERVICE`; the description fact check in the kernel slot `DESCRIPTION_FACT_CHECK_HOST`; the scheduler stopped and both removed at shutdown | `features/registry.py` |
| `health()` | `depmap-assets`: the page template and script in the wheel | health block |

### Cross-feature and core use

The core's description refresh (`global_repos/meta_description_hook.py`)
used the dependency-map analyzer's verification pass (Story #724 v2). It now
asks the kernel slot `kernel.capabilities.DESCRIPTION_FACT_CHECK_HOST`
(`DescriptionFactCheckHost`), which the feature fills at `on_startup`;
without the feature descriptions are written unverified. No other feature
uses the dependency map.

The running service is the feature's alone: `on_startup` installs it in
`cidx_depmap.server.runtime.SERVICE` (a `CapabilitySlot`, like the context
slot) once its scheduler runs, `on_shutdown` empties it, and the tools and
pages read `runtime.dependency_map_service()` (None when it did not start:
the trigger tool and pages answer "unavailable", the read tools "dep_map_path
not found"). The core needs nothing from the service: its job-cancel handlers
reach it through `BackgroundJobManager.register_cancel_handler`, and the fact
check through the kernel slot above. Tests install a service with
`tests/unit/server/_depmap_feature.py` (`depmap_service_installed`,
`install_depmap_service`, `depmap_started`).

## Catalog access projection

The tools and pages never decide what a caller may see of the dependency map:
`mcp/handlers._resolve_source` and `web/caller_view.resolve_caller_view` ask
`access_projection.dependency_map_access(viewer, map_dir)` and serve through
the returned `DependencyMapView` (`restricted`, `visible_repositories`). The
core binds it to `resolve_meta_visibility` + `source_for_visibility`
(`server/features/access_projection.py`), the same projection the file doors
apply. `tests/unit/ci/test_catalog_guard_presence.py` requires the core
binding, the two feature call sites, every `depmap_*` tool's call and every
data route's caller view; `catalog_guard_known_gaps.json` stays `[]`.

## Configuration

These `claude_integration_config` fields are the `depmap` section now
(`feature_settings["depmap"]`, runtime): `dependency_map_enabled`,
`dependency_map_interval_hours`, `dependency_map_pass_timeout_seconds`,
`dep_map_fact_check_enabled`, `fact_check_timeout_seconds`,
`dependency_map_pass{1,2}_max_turns`, `dependency_map_delta_max_turns`,
`refinement_enabled`, `refinement_interval_hours`,
`refinement_domains_per_run`, `dep_map_auto_repair_enabled`. On load,
`FEATURE_OWNED_NESTED_KEYS` moves stored values out of
`claude_integration_config` into the section; a value already in the section
wins. The section declares the bounds the former Claude CLI form had
(`fact_check_timeout_seconds` 60..3600, `refinement_domains_per_run` 1..50, the
others a lower bound only); the feature config door refuses a value outside
them where the old form clamped it. `dependency_map_enabled` is
`restart_required` (the scheduler starts at boot).
There are no bootstrap keys (the graph-repair flag `enable_graph_channel_repair`
stays a core bootstrap key).

## Import contracts

| Contract | Baseline |
|---|---|
| `core-does-not-import-depmap-feature` | 0 |
| `depmap-feature-no-core` | the edges of `cidx_depmap._platform` only (lazy names, typed under `TYPE_CHECKING`) |
| `depmap-feature-no-other-feature`, and the other features' `*-no-other-feature` | 0 |
| `library-packages-no-server` | lowered: `global_repos.dependency_map_analyzer` left `global_repos`, and `meta_description_hook` no longer imports the config service |

## Not done yet

- The storage backends, the catalog dependency-map parsers and the CLI
  dispatcher factory are reached through `_platform`; they become SDK
  capabilities (storage, catalog format, CLI invoker).
- The repository pre-check and the analysis agent allowlist name the tools; a
  `ToolContribution` field would let the feature declare them.
