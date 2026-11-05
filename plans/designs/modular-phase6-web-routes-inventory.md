# Modular phase 6: `web/routes.py` inventory and split

Phase 6 of [the add-on plan](modular-packaging-addons.md) splits the web UI into per-feature route modules so feature
pages can later ship inside their add-ons ([end state](modular-endstate.md), section 8 and acceptance criterion 5).
This step is mechanical: every route, helper and constant moved unchanged into a per-domain module; behaviour, route
paths, `url_for` names, dependencies and the matching order of overlapping routes are unchanged.

## Before

`server/web/routes.py`: 13,368 lines, 119 routes on four routers that the app mounts in
`routers/inline_routes.py`:

| Router | Mounted at | Routes |
|---|---|---|
| `web_router` | `/admin` | 104 |
| `user_router` | `/user` | 9 |
| `login_router` | `/` | 5 |
| `api_router` | `/api` | 1 |

Imported from outside the module: `web/__init__.py` (the four routers), `auth/oidc/routes.py`
(`_get_user_mfa_status`), `auth/oauth/routes.py` (`templates`), `routers/diagnostics.py` (CSRF helpers),
`services/golden_catalog_sync.py` (`_get_token_manager`), `startup/lifespan.py`
(`shutdown_discovery_branch_fetch_executor`), and the sibling web modules (`repo_category_routes`,
`dependency_map_routes`, `audit_log_routes`, `siem_delivery_routes`, `siem_forms`, `mfa_routes`). Tests import or patch
about 100 of its names.

## After

`web/routes.py` (about 400 lines) aggregates the page modules under `server/web/pages/`. Each page module has its own
`APIRouter` per mount point (named after the router it feeds: `web_router`, `user_router`, ...); the aggregator
includes them into the four exported routers in the order the routes were first registered.

| Module | Kind | Lines | Lines moved | Routes | Paths |
|---|---|---|---|---|---|
| `pages/_shared.py` | shell | 473 | 384 | 0 | templates, logger, CSRF, session gates, service accessors |
| `pages/session.py` | shell | 677 | 606 | 8 | `/login*`, `/admin/logout`, `/user/logout`, `/admin/login`, `/user/login`, `/api/server-time` |
| `pages/dashboard.py` | platform | 879 | 820 | 12 | `/admin/`, `/admin/partials/dashboard-*`, `/admin/admin/cache-sample`, `/admin/langfuse-sync/trigger` |
| `pages/users.py` | platform | 533 | 482 | 7 | `/admin/users*`, `/admin/partials/users-list` |
| `pages/groups.py` | platform | 855 | 775 | 10 | `/admin/groups*`, `/admin/partials/groups-*` |
| `pages/golden_repos.py` | platform | 1,424 | 1,310 | 13 | `/admin/golden-repos*`, `/admin/partials/golden-repos*` |
| `pages/activated_repos.py` | platform | 483 | 430 | 4 | `/admin/repos*`, `/admin/partials/repos-list` |
| `pages/jobs.py` | platform | 451 | 407 | 4 | `/admin/jobs*`, `/admin/partials/jobs-list`, `/admin/api/queue-status` |
| `pages/config.py` | platform | 998 | 909 | 9 | `/admin/config*`, `/admin/partials/config-section` |
| `pages/config_values.py` | platform | 1,866 | 1,831 | 0 | current config values, section validation, restart-required fields |
| `pages/siem_config.py` | platform | 269 | 228 | 4 | `/admin/config/siem_delivery/*` |
| `pages/credentials.py` | platform | 726 | 627 | 18 | `/admin/settings/git`, API keys, MCP and git credentials (admin and `/user/*`) |
| `pages/ssh_keys.py` | platform | 306 | 269 | 4 | `/admin/ssh-keys*` |
| `pages/logs.py` | platform | 284 | 251 | 3 | `/admin/logs*`, `/admin/partials/logs-list` |
| `pages/embedding_stats.py` | platform | 91 | 70 | 1 | `/admin/embedding-stats` |
| `pages/self_monitoring.py` | platform | 696 | 636 | 3 | `/admin/self-monitoring*` |
| `pages/analytics_export.py` | platform | 66 | 43 | 1 | `/admin/analytics-export` |
| `pages/restart.py` | platform | 242 | 207 | 1 | `/admin/restart` |
| `pages/discovery.py` | tool family | 1,020 | 868 | 10 | `/admin/auto-discovery`, `/admin/api/discovery/*`, `/admin/partials/auto-discovery/*` |
| `pages/query.py` | tool family | 1,043 | 980 | 4 | `/admin/query`, `/admin/partials/query-results` |
| `pages/scip_query.py` | feature (SCIP) | 297 | 274 | 0 | SCIP query execution for the query page |
| `pages/wiki.py` | feature (wiki) | 211 | 179 | 3 | `/admin/golden-repos/{alias}/wiki-*`, `/admin/activated-repos/{username}/{alias}/wiki-toggle` |

"Lines moved" counts the moved definitions with their comments; "Lines" adds each module's imports and docstring.
`routes.py` had no X-Ray, memory or dependency-map pages; the dependency-map pages are in
`web/dependency_map_routes.py`, which is unchanged apart from its imports.

## Placement rules

- Route handlers stay with their domain. The three wiki actions moved out of the golden and activated repository
  blocks into `wiki.py`.
- A helper used by one module moved with it. A helper used by two or more modules went to `_shared.py`, except where a
  domain owns it: `_create_golden_repos_page_response` (golden repos, used by `wiki`), `_get_all_activated_repos`
  (activated repos, used by `query`), `_create_config_page_response` (config, used by `siem_config`) and the
  configuration data in `config_values.py` (used by `config` and `dashboard`).
- `_get_user_mfa_status` is in `_shared.py` so `auth/oidc/routes.py` can import it without loading the login page or
  the OIDC reload code.

## Cross-imports between page modules

Every page module imports its gates and rendering helpers from `_shared` (`_require_admin_session`,
`_create_login_redirect`, `templates`, `logger`, the CSRF helpers, and the service accessors it needs:
`_get_golden_repo_manager`, `_get_activated_repo_manager`, `_get_background_job_manager`, `_get_group_manager`,
`_get_users_list`, `_get_repo_category_service`, `_get_token_manager`, `_get_ssh_key_manager`). The other edges:

| Importer | Imports from | Names |
|---|---|---|
| `config` | `config_values` | `RESTART_REQUIRED_FIELDS`, `_VALID_CONFIG_SECTIONS`, `_get_current_config`, `_get_qec_total_entries`, `_validate_config_section` |
| `dashboard` | `config_values` | `_get_qec_total_entries` |
| `siem_config` | `config` | `_create_config_page_response` |
| `wiki` | `golden_repos` | `_create_golden_repos_page_response` |
| `query` | `activated_repos` | `_get_all_activated_repos` |
| `query` | `scip_query` | `_execute_scip_query`, `_record_web_scip_search` |
| `scip_query` | `_shared` | `_detect_language_from_path`, `logger` |

No shell or platform module imports `wiki` or `scip_query`; `_shared` imports no page module.

## Feature dependencies that remain

- `query.py` (the query tool family) imports `scip_query` and keeps eight function-level imports of
  `code_indexer.scip` inside `query_submit` and `query_results_partial_post` (the SCIP branch of each handler). Moving
  them needs the handlers split by search mode, which is a behaviour-level change, not a move.
- `config_values.py` holds the `wiki`, `xray` and dependency-map configuration values and their validation, and the
  configuration page renders them. Feature configuration schemas move with the features (end state section 10), not
  with this split.
- `jobs.py` lists dependency-map jobs from the job tracker alongside the other background jobs.
- The golden and activated repository pages render the wiki toggle buttons from their templates.

## Aggregator

`web/routes.py` re-exports every name that source modules or tests import or patch from
`code_indexer.server.web.routes`, so those imports keep working. Tests replace helpers by patching that module
(`mock.patch("code_indexer.server.web.routes._require_admin_session")`, `monkeypatch.setattr(routes, ...)`). Before
the split the patched name and its callers shared one namespace; now the module's class rebinds an assigned name in
every page module whose binding is the replaced object, so a patch reaches the same call sites as before. Three names
that the page code rebinds with `global` (`_restart_in_progress`, `_DISCOVERY_BRANCH_FETCH_EXECUTOR`,
`_DISCOVERY_BRANCH_FETCH_GATE`) are read from and written to their owning module instead of being copied.

The logger keeps its name (`code_indexer.server.web.routes`) so log filters and searches are unaffected.

## Import cycle

`auth/oidc/routes.py` imported `web.routes` at module level, and `web/__init__.py` imported `web.routes`, which imports
`auth` and `routers` modules back. Now `web/__init__.py` loads the four routers on first attribute access, and
`auth/oidc/routes.py`, `routers/diagnostics.py`, `web/repo_category_routes.py`, `web/dependency_map_routes.py` and
`web/siem_delivery_routes.py` import from `web.pages._shared`. Importing `auth.oidc.routes` no longer loads any page
module. Function-level imports of `web.routes` (`mfa_routes`, `auth/oauth/routes.py`, `golden_catalog_sync`,
`audit_log_routes`, `siem_forms`) are unchanged.

## Verification

- `tests/unit/server/web/test_web_route_table_snapshot.py` compares the four routers with a snapshot recorded from the
  single-module `routes.py`: methods, path, name, endpoint, response class, status code, schema visibility,
  parameters and dependency set of every route, and the relative order of every pair of routes that can match the
  same request. `user_router`, `login_router` and `api_router` keep their exact order; in `web_router` only the three
  wiki routes moved, and none of them overlaps another route.
- The import-contract ratchet entries for `web.routes -> code_indexer.scip.*` (13 lines) are renamed to
  `web.pages.query` (8) and `web.pages.scip_query` (5).

## Known issues carried over

- `query.py` keeps the per-user query history in a module-level dict (per node, not cluster-aware); marked with a
  `TODO`.

## Next steps

- Feature pages (`wiki`, `scip_query`, then the query tool family) register through the feature's `ui_pages()` once
  feature-router inclusion lands, instead of through the aggregator.
- Page modules call application services through `FeatureContext` instead of `_shared` accessors.
