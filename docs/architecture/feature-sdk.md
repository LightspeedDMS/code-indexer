# Feature SDK and Loader

Status: phases 3 and 4a of the modular packaging plan (SDK 0.7 since phase 4f) (the modular end-state
and packaging designs under `plans/designs/`). This page describes what exists
in the code today, and marks what is still only proposed.

## What exists

| Piece | Where | State |
|---|---|---|
| `cidx-sdk` distribution (import name `cidx_sdk`) | `packages/cidx-sdk/` | Built, version 0.7.0, zero runtime dependencies |
| Feature contract (`Feature`, `FeatureManifest`, contribution DTOs) | `cidx_sdk/feature.py` | Defined and validated |
| Capability names, `FeatureContext` | `cidx_sdk/capabilities.py` | Every name provided by the core |
| Capability Protocols | `cidx_sdk/capability_types.py` | One per name (`CAPABILITY_PROTOCOLS`) |
| Slot type, web request-state contract | `cidx_sdk/slots.py`, `cidx_sdk/web.py` | Used by the kernel and by feature routes/pages |
| Entry-point group names, version check | `cidx_sdk/entry_points.py`, `version.py` | As in phase 3 |
| Seam Protocols, DTOs, errors, resolver | `cidx_sdk/seams/` | Phase 5 (0.3, 0.4); see [seams.md](seams.md) |
| Loader | `src/code_indexer/server/features/loader.py` | Discovers, validates, orders |
| Registry, contexts, health block | `src/code_indexer/server/features/registry.py` | Runs startup/shutdown, reports health |
| Core capability providers | `src/code_indexer/server/features/capabilities.py` | One per capability name |
| Hook consumers | `server/features/{tools,web,config,migrations,jobs,dispatch_policy,index_builders,query_modes,search_augmenters}.py`, `code_indexer/cli_features.py` | Every hook consumed |
| Wiring | `src/code_indexer/server/startup/feature_wiring.py` | `wire_features` from `create_app`; three lifespan helpers |
| Config keys | `enabled_features`, `feature_bootstrap_settings` (bootstrap); `feature_settings` (runtime) | `ServerConfig`, `BOOTSTRAP_KEYS` |

X-Ray (`cidx-xray`, phase 4c), SCIP (`cidx-scip`, phase 4d), the wiki
(`cidx-wiki`, phase 4e), shared technical memories (`cidx-memory`, phase 4f)
and the dependency map (`cidx-depmap`, phase 4g) are their own distributions
and removable add-ons: the core consumes every hook they contribute and
imports none of them (see [cidx-xray.md](cidx-xray.md),
[cidx-scip.md](cidx-scip.md), [cidx-wiki.md](cidx-wiki.md),
[cidx-memory.md](cidx-memory.md), [cidx-depmap.md](cidx-depmap.md)). The other
tool families still live in the core.

## The SDK

Pure interfaces, semver, no third-party imports. Where the core needs a
framework object (a FastAPI router, a Click command) the field is typed
`object` and documented; the core checks it when it consumes the contribution.

| Name | Purpose |
|---|---|
| `SDK_VERSION` | The running SDK version |
| `VersionRange`, `version_satisfies`, `sdk_satisfies`, `parse_version` | Range check |
| `FeatureManifest` | name, version, `requires_sdk`, `required_capabilities`, `optional_seams`, `depends_on`, description |
| `Feature` (Protocol), `BaseFeature` | The hooks; `BaseFeature` returns empty defaults |
| `ToolContribution`, `RouterContribution`, `UiPageContribution`, `CliCommandContribution`, `ConfigSection`/`ConfigKey`, `Migration`, `BackgroundJob`, `HealthReport` | Hook return types |
| `JobTypeContribution`, `IndexBuilderContribution` (`IndexBuildRequest`, `IndexBuildOutcome`), `WebQueryModeContribution` (`WebQueryRequest`, `WebQueryOutcome`) | Hook return types (0.5) |
| `SearchAugmenterContribution` (`SearchAugmentRequest`) | Hook return type (0.7) |
| `FeatureContext` (Protocol), `KNOWN_CAPABILITIES` | What a feature receives at startup; capability names |
| `CAPABILITY_PROTOCOLS` and the `*Capability` Protocols | The type of each capability (0.2) |
| `CapabilitySlot` | Process-wide install slot (the kernel's interfaces use it) (0.2) |
| `Principal`, `PageRenderer`, `PRINCIPAL_STATE_ATTR`, `RENDER_STATE_ATTR` | What a feature route or page finds on `request.state` (0.2) |
| `FEATURES_ENTRY_POINT_GROUP`, `SEAM_ENTRY_POINT_GROUPS`, `require_seam_provider` | Discovery and seam refusal |
| `FeatureLoadError` and subclasses, `SeamUnavailableError`, `InvalidManifestError`, `CapabilityUnavailableError` | Errors |

0.2 is a MINOR bump (additions only): new capability names (`content_seam`,
`regex_target`, `snapshot_predicate`, `read_scope`), the capability Protocols,
`RouterContribution.required_permission`. While MAJOR is 0 a minor bump counts
as breaking, so an add-on built for `>=0.1,<0.2` is refused by this core.

0.3 is a MINOR bump (additions only): `cidx_sdk.seams`, one module per seam
with its Protocol, DTOs and error types, the content identity hash, the seam
resolver (`SeamResolver`, `SeamPluginError`) and the clone seam's entry-point
group. See [seams.md](seams.md). An add-on built for `>=0.2,<0.3` is refused.

0.4 is a MINOR bump (additions only): what local backends are handed by the
core (`GitProcessRunner`, `BoundedProcessRunner`, `CloneBackendSettings` /
`CloneServices`), the CLI invoker's result types (`InvocationResult`,
`FailureClass`) and its `REPOSITORY_SOURCE` slot, the content writer's `UNSET`
and `ContentWriteError`, `index_root_for`, and helpers moved out of the core
(`redact_url_credentials`, the snapshot deletion errors,
`SubprocessCancelledError`). An add-on built for `>=0.3,<0.4` is refused.

0.5 is a MINOR bump (additions only): the per-tool and per-job dispatch
policy a feature declares instead of the core naming its tools
(`ToolContribution.manages_own_timeout`, the `job_types()` hook with
`JobTypeContribution.hidden_from_dashboard` / `repo_alias_optional`), the
indexing pipeline's extension point (`index_builders()`), the Web UI query
page's search modes (`web_query_modes()`), and numeric bounds on a config key
(`ConfigKey.minimum` / `maximum`, enforced on runtime updates). An add-on built
for `>=0.4,<0.5` is refused; cidx-xray moved to `>=0.5,<0.6`.

0.6 is a MINOR bump (additions only): the capabilities `access_projection`
(`AccessProjectionCapability`, `CatalogArticleView`: the catalog access
projection a feature serving repository files applies) and `repository_events`
(`RepositoryEventsCapability`, `RepositoryEventListener`: content changed,
repository removed or renamed), and `RouterContribution.auth`
(`ROUTER_AUTH_API`, `ROUTER_AUTH_HYBRID`), `login_redirect` and `static_dir`.
An add-on built for `>=0.5,<0.6` is refused.

0.7 is a MINOR bump (additions only): the `search_augmenters()` hook
(`SearchAugmenterContribution`, `SearchAugmentRequest`: a section a feature
adds to the MCP `search_code` response, handed the query embedding the core
computed once for the search), `AccessProjectionCapability.visible_memory_ids`
(the catalog's memory-file scope rule) and `AccessProjectionCapability.dependency_map_access`
with `DependencyMapAccess` / `DependencyMapView` (a caller's projected view of
the catalog's dependency map; added in phase 4g, same unreleased 0.7) and
`RouterContribution.nav_label` / `nav_permission` (a router's page in the
shell navigation, for a page that keeps its own auth and URL), and
`cidx_sdk.tool_args` (`coerce_int`, `coerce_float`: the optional numeric MCP
argument reading the core's and the features' handlers share), and the config
screen's presentation of a section: `ConfigKey.label` / `restart_required`
and `ConfigSection.title` / `description`. An
add-on built for `>=0.6,<0.7` is refused; cidx-xray, cidx-scip, cidx-wiki,
cidx-memory and cidx-depmap pin `>=0.7,<0.8`.

Hooks are synchronous. The core calls them from a worker thread (or while the
app is being built, before it serves requests), never on the event loop.

## Writing a feature

```toml
[project]
name = "cidx-example"
dependencies = ["cidx-sdk>=0.7,<0.8"]

[project.entry-points."code_indexer.features"]
example = "cidx_example:make_feature"
```

The entry point name is the feature name and must equal `manifest.name`. The
value is a zero-argument factory returning a `Feature`. The add-on uses its own
top-level import name and imports `cidx_sdk` (plus the frameworks its
contributions are made of: FastAPI, Click), never `code_indexer`. The test
feature `tests/feature_fixtures/cidx_sample_feature/` contributes one of every
hook and is driven end to end by
`tests/unit/server/features/test_feature_front_door.py`; it is not shipped.

A feature's server side is ON when it is installed AND named in
`enabled_features`. Enabled but not installed stops startup. Its CLI commands
are available whenever it is installed (see `cli_commands()` below): they run
on the user's machine and need no server.

A source-tree run (no installed metadata) finds the in-tree features through
`cidx_testkit.registry`, which registers what the in-tree distributions
declare with the SDK (`cidx_sdk.seams.resolver.add_entry_points`);
`discover_installed_features` reads those registrations after the installed
distributions, an entry of the same name replacing an installed one.

## Loading and wiring

1. `create_app()` registers the core routes, then calls `wire_features(app,
   services)`: read `enabled_features`, discover, load and validate (the
   refusals below), hand each feature a context over the core capabilities,
   then consume every hook (next section). Any refusal stops startup.
2. The lifespan calls `load_configured_features` (publishes the registry for
   health), `start_features` (each `on_startup` in dependency order, then the
   job scheduler) just before it yields, and `stop_features` (scheduler, then
   each `on_shutdown` in reverse) first after the yield.

| Condition | Error |
|---|---|
| Enabled but not installed | `FeatureNotInstalledError` |
| `requires_sdk` excludes the running SDK | `IncompatibleSdkError` |
| A required capability is not provided | `MissingCapabilityError` |
| `depends_on` names a feature that is not enabled, or a cycle | `FeatureDependencyError` |
| Import fails, factory raises, not a `Feature`, manifest name mismatch, a hook returns the wrong type, duplicate MCP tool name, migrations not strictly increasing | `FeatureLoadError` |
| Invalid tool doc, a tool or route colliding with the core, a router that is not an `APIRouter`, a page path its router does not serve | `FeatureLoadError` (at consumption) |
| A migration raises | the migration's error (rolled back) |
| `on_startup` raises (features already started are stopped in reverse) | `FeatureStartupError` |

## How each hook is consumed

| Hook | Consumed by | Result |
|---|---|---|
| `tools()` | `features/tools.py` `install_feature_tools` | `TOOL_REGISTRY` entry built from the wheel's doc by the core's tool-doc parser, `HANDLER_REGISTRY` entry, doc in the tool-doc loader. Listed and authorized exactly like a core tool (role permission, group grants, repository pre-check). Rebuilt on every app build, so a disabled feature's tools disappear |
| `routers()` | `features/web.py` `install_feature_web` | Included under `prefix`, in contribution order; every route needs an authenticated caller holding `required_permission`: bearer, API key or session JWT (`auth="api"`), or a Web UI session first, then bearer or API key (`auth="hybrid"`); `login_redirect` turns a 401 into a 303 to `/login?redirect_to=...`; `request.state.cidx_principal` is set; a router's `static_dir` is served unauthenticated at `<prefix>/_static/`, mounted before the routers; a router's `nav_label` is a nav entry linking to `<prefix>`, shown to sessions holding `nav_permission` |
| `ui_pages()` | `features/web.py` | Included behind a Web UI session (else redirect to `/login`) and `required_permission`; `request.state.cidx_render` renders the page's templates inside the shell (`{% extends shell_template %}`); `static_dir` served at `/feature-static/<feature>/`; nav entry in `base.html` / `user_base.html` for sessions holding the permission |
| `cli_commands()` | `code_indexer/cli_features.py`, `cli.ModeAwareGroup` | Consulted only for a name that is not built in, and for help, so built-in commands import nothing (test: `tests/unit/cli/test_cli_features.py`). Every INSTALLED feature counts, independent of `enabled_features` (phase 4c); each is loaded on its own (`loader.load_cli_commands`: manifest and `cli_commands()` only, never the server hooks), so one refused add-on hides no other; a name nobody provides while an add-on was refused fails loud, help warns. `on_startup` never runs in the CLI |
| `config_section()` | `features/config.py`; doors in `features/web.py` (mounted by `app_wiring`) | Runtime keys in `feature_settings[<feature>]` (database, compare-and-set, one `config_changed` audit row on `feature_settings.<feature>`); bootstrap keys in `feature_bootstrap_settings[<feature>]` (`config.json`, read-only on the server). `GET /api/admin/features/config`, `PUT /api/admin/features/{feature}/config` (admin, elevation), the "Feature Settings" block of `/admin/config` (`partials/feature_settings.html`) posting to `POST /admin/features/config/{feature}`; all three commit through `features/config.commit_runtime_update`. The block is the only place a feature's settings are shown or edited: it titles and describes the section, shows each key's `label` (the name below it), a number input bounded by `minimum`/`maximum`, a checkbox for a bool, text otherwise, a bootstrap key read-only, and a "Requires server restart" note for `restart_required`. The core's own sections, categories and validation name no feature key |
| `migrations()` | `features/migrations.py`; PostgreSQL via `MigrationRunner.run_feature_migrations` | Run at app build after the core schema, in dependency order; PG under the runner's session advisory lock, SQLite one `BEGIN IMMEDIATE` per migration; one row per feature in `feature_schema_versions`. A row newer than the feature (downgrade) applies nothing |
| `background_jobs()` | `features/jobs.py`, the `background_jobs` capability | BackgroundJobManager jobs (dashboard, `/api/jobs`), deduplicated cluster-wide by a feature-namespaced sentinel alias on the job tracker's active-job index. On demand: `run_registered(name)`. Recurring: one ticker per job, first run after one interval, the other nodes skip the tick |
| `health()` | `features/registry.py` | The `features` block of `GET /health`, `GET /api/system/health` and MCP `check_health`, plus reports pushed through the `health` capability |
| `job_types()`, `ToolContribution.manages_own_timeout` | `features/dispatch_policy.py` | The dashboard leaves hidden types out of recent jobs (`services/dashboard_service.py`); the job manager expects no `repo_alias` for `repo_alias_optional` types (`repositories/background_jobs.py`); the MCP dispatcher applies no generic timeout to a tool that bounds its own runtime (`mcp/protocol.py`) |
| `index_builders()` | `features/index_builders.py` -> `kernel/index_builders.py` | The golden-repo refresh, add-index, the activated reindex and `get_index_status` build and describe a contributed index type through it; a type with no builder is skipped (refresh) or refused (add-index). A core type (`semantic`, `fts`, `temporal`) cannot be contributed |
| `web_query_modes()` | `features/query_modes.py` | The query page offers the mode and hands a request in it to the feature (the core resolved the repository); without a contributor the mode is refused |
| `search_augmenters()` | `features/search_augmenters.py` | The MCP `search_code` single-repository path asks which sections apply to the search's mode and are active (before the search, so the query embedding is computed once when any wants it), then hands each the finished search and answers its section under `query_metadata[key]`; a section that raises or answers None adds nothing. Two features answering under one key are refused |
| `on_startup` / `on_shutdown` | `features/registry.py` | Once per process, dependency order / reverse |

Feature health: a report that is not healthy (even `unhealthy`) makes the
overall status DEGRADED with a `Feature <name> (<component>): ...` reason,
never UNHEALTHY. An add-on's trouble does not take the node out of the load
balancer.

## Capabilities

`build_core_capabilities` (`features/capabilities.py`) provides every name;
each object is built per feature on first use and resolves its service from
`app.state` when called (a service that is not running raises `RuntimeError`
naming it).

| Capability | Core implementation |
|---|---|
| `config` | `get_config_service().get_config()`; `section()` from `features/config.py`; `subscribe()` via `register_on_commit_callback` |
| `payload_cache` | `app.state.payload_cache`; keys namespaced `feature:<name>:<key>` |
| `background_jobs` | `app.state.background_job_manager.submit_job` with a namespaced dedup alias; `get_job_status` |
| `telemetry` | `kernel_wiring.ServerTelemetryHost` |
| `http_client` | `app.state.http_client_factory` (fault-injection aware) |
| `storage` | PG pool `connection()` in cluster mode, the server SQLite database solo |
| `permissions` | live account from `app.state.user_manager`, `User.has_permission` |
| `access_filter` | `app.state.access_filtering_service` (same decision as the MCP repository pre-check) |
| `audit_log` | `audit_capture.capture` against the core catalog (an unknown action type is a reported drop) |
| `search_events` | `record_search_event` on the app's search-event writer |
| `health` | reports kept by the registry (per node, like all health input) |
| `content_seam`, `regex_target`, `read_scope` | `kernel_wiring` server hosts |
| `access_projection` | `features/access_projection.py`: the catalog access projection (cidx-meta), applied to a caller's view; one place, above the seam. `visible_memory_ids` is the access-filtering service's memory scope rule, `dependency_map_access` the dependency-map projection (0.7) |
| `repository_events` | `features/repository_events.py`: listeners per process, told by the file and git write tools, write-mode exit, golden repository removal and rename; a listener's failure never fails the operation |
| `snapshot_predicate` | `code_indexer.utils.snapshot_paths` |

The kernel's own interfaces (`code_indexer.kernel.capabilities`) now re-export
the SDK Protocols; X-Ray and SCIP keep calling the kernel helpers.

What a feature needs from the server beyond the SDK Protocols (the job
tracker lifecycle, paged payload-cache writes, the X-Ray search-event
recorder, the group-based repository guard, repository resolution, query
admission, content-seam manifest reads, cidx-meta writes, a resizable limiter,
the memory governor's composite cache) is the kernel slot
`SERVER_PLATFORM_HOST` (`kernel.capabilities.ServerPlatformHost`), filled by
`server/feature_platform.py` when the server package is imported. It is
transitional: each method group is a candidate SDK capability.

## Packaging

The core depends on the SDK by path:
`"cidx-sdk @ {root:uri}/packages/cidx-sdk"` (hatchling context formatting).
`pip install .`, `pip install -e .`, the CI smoke job and the Dockerfile build
resolve it with no extra step. mypy reads the SDK from `packages/cidx-sdk/src`.
Registry publishing comes later (see the decisions in
`plans/designs/modular-packaging-addons.md`).

## Not done yet

- Moving the remaining tool families out of the core (CI/CD, SSH keys, pull
  requests), by the same rule.
  `kernel/feature_handoff.py` stays for the next one; no feature is
  core-wired now, and the config screen no longer skips a core-wired
  feature's section: a feature handed off later brings its settings to the
  Feature Settings block like every other.
- SDK capabilities for what `ServerPlatformHost` still lends (see above).
- Seam Protocols and resolvers in the SDK; seam fail-loud selection.
- A feature cannot add audit catalog types yet; it records core types.
- Feature tool categories appear in the quick reference only when they reuse
  an existing core category.
- Feature routes are checked for collisions against the routes registered at
  app build; routers the lifespan mounts later are not in that check.
- pyinstaller: unchanged and already stale; features are server-side.
