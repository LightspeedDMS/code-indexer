# cidx-wiki: the wiki as a feature distribution

Status: phase 4e of the modular packaging plan
([modular-endstate.md](../../plans/designs/modular-endstate.md)). The wiki is a
removable add-on built the way X-Ray is ([cidx-xray.md](cidx-xray.md)): the
core imports no `cidx_wiki` module and wires nothing of the wiki itself; it
consumes the feature's hooks. The feature contract is in
[feature-sdk.md](feature-sdk.md); what the wiki does for administrators is in
[docs/server/wiki.md](../server/wiki.md).

## Turning it on and off

| What | Rule |
|---|---|
| Server side (`/wiki` pages and static files, `/admin` wiki actions, MCP `wiki_article_analytics`, config section, migration, cache invalidation, health) | installed AND `"wiki"` in the bootstrap key `enabled_features` (`config.json`) |
| Enabled but not installed | startup refused (`FeatureNotInstalledError`) |
| Installed, not enabled | no wiki routes, tool or invalidation; `GET /health` lists `wiki` under `installed_not_enabled`; the repository pages show no wiki controls and MCP search results carry no `wiki_url` (`features.registry.feature_offered`) |
| Not installed | no wiki anywhere; the core starts and runs unchanged |

The wiki has no CLI commands. The core depends on `cidx-wiki` by path
(`pyproject.toml`), so the default install and the image carry it. Example
`config.json`:

```json
{"enabled_features": ["xray", "wiki"]}
```

Data is kept whichever state: the repositories' `wiki_enabled` flags are core
repository metadata, and the wiki tables stay (inert) when the feature is off.

## What exists

| Piece | Where |
|---|---|
| Distribution `cidx-wiki` (import name `cidx_wiki`, version 0.1.0) | `packages/features/cidx-wiki/` |
| Feature object `wiki` | `cidx_wiki/feature.py`, entry point `wiki = "cidx_wiki.feature:make_feature"` |
| Config section | `cidx_wiki/config.py` (`WikiConfig` lives here now) |
| Routes, templates, static files | `cidx_wiki/server/routes.py`, `templates/`, `static/` |
| Admin actions (toggle, cache refresh, user-wiki toggle) | `cidx_wiki/server/admin_routes.py` |
| MCP handler and tool doc | `cidx_wiki/server/mcp/analytics.py`, `cidx_wiki/server/tool_docs/wiki_article_analytics.md` |
| Rendering, cache, content seam, invalidator | `cidx_wiki/server/{wiki_service,wiki_cache,wiki_content_seam,wiki_cache_invalidator}.py` |
| Catalog access projection, applied | `cidx_wiki/server/catalog_view.py` |
| Migration | `cidx_wiki/server/migrations.py` |
| `on_startup` / `on_shutdown` | `cidx_wiki/server/lifecycle.py`, `runtime.py` |
| The one core adapter | `cidx_wiki/_platform.py` |
| Tests | `packages/features/cidx-wiki/tests/`; core integration tests stay in `tests/unit/server/wiki/` (they import `cidx_wiki`); removability: `tests/unit/server/features/test_wiki_removability.py` |

Moved (old core path -> new):

| Old | New |
|---|---|
| `server/wiki/routes.py` | `cidx_wiki/server/routes.py` |
| `server/wiki/catalog_wiki.py` (the catalog projection for the wiki) | core `server/services/cidx_meta_access/catalog_wiki.py` (projection only; the wiki renders) |
| `server/wiki/{wiki_service,wiki_cache,wiki_cache_invalidator,wiki_content_seam}.py`, `templates/`, `static/` | `cidx_wiki/server/` |
| `server/web/pages/wiki.py` | `cidx_wiki/server/admin_routes.py` |
| `handle_wiki_article_analytics` and helpers in `server/mcp/handlers/guides.py` | `cidx_wiki/server/mcp/analytics.py` |
| `server/mcp/tool_docs/search/wiki_article_analytics.md` | `cidx_wiki/server/tool_docs/` |
| `ServerConfig.wiki_config` / `WikiConfig` | the `wiki` config section / `cidx_wiki.config.WikiConfig` |
| Invalidator wiring in `startup/app_wiring.py`; cache calls in `git_write.py`, `files.py`, `golden_repo_manager.py`, `golden_repo_rename.py` | `repository_events` (core publishes, the feature listens) |

Stayed in the core: the `wiki_enabled` columns and their manager methods, the
wiki cache storage backends (`storage/{protocols,sqlite_backends,postgres}/wiki_cache_backend.py`,
`BackendRegistry.wiki_cache`, reached through `ServerPlatformHost.backend_registry_attr`),
the article HTML allowlist (`utils/html_sanitizer.py`), the `/wiki` CSP in
`middleware/security_headers.py`, and the `wiki_url` enrichment of search
results (`mcp/handlers/_utils.py`, off while the feature is not loaded).

## How each hook is consumed

| Hook | Contribution | Core consumer |
|---|---|---|
| `manifest` | `requires_sdk` `>=0.7,<0.8` (SDK 0.6 adds `access_projection`, `repository_events` and the router `auth`/`login_redirect`/`static_dir` fields); capabilities `config`, `permissions`, `access_projection`, `repository_events` | loader |
| `tools()` | `wiki_article_analytics`, doc from the wheel | `features/tools.py` |
| `routers()` | 1. `/wiki` search routes (`auth="hybrid"`, 401); 2. `/wiki` pages and assets (`auth="hybrid"`, `login_redirect`, `static_dir` served at `/wiki/_static/`); 3. `/admin` wiki actions (`auth="hybrid"`, `manage_users`; each action also requires elevation, the admin session and its CSRF token) | `features/web.py` |
| `config_section()` | runtime `enable_header_block_parsing`, `enable_article_number`, `enable_publication_status`, `enable_views_seeding` (all `true`), `metadata_display_order` (`""`) | `features/config.py`; shown and edited only in the generic Feature Settings block ("Wiki Configuration") |
| `migrations()` | 1: the wiki tables (imports core 001/024) | `features/migrations.py` |
| `on_startup` / `on_shutdown` | context; invalidator subscribed to `repository_events` | `features/registry.py` |
| `health()` | `wiki-assets`: templates and static files present | health block |

The search routes are contributed first: FastAPI matches in order, and the
article catch-all `/{repo_alias}/{path:path}` would take `_search` as a path.
`routes.wiki_router` still holds every route in the original order, with the
per-route dependencies `get_wiki_user_hybrid` / `get_current_user_hybrid`
reading the principal the core put on the request; tests that mount the
router directly override them.

### What the wiki reaches, and how

| Need | Through |
|---|---|
| wiki settings | `config` capability (`runtime.wiki_config()`) |
| another permission of the caller | `permissions` capability (`routes.WikiCaller`) |
| the catalog view of a caller | `access_projection` capability (`catalog_view.restricted_catalog_view`) |
| cache invalidation on repository changes | `repository_events` capability |
| the shared cache store, the SQLite connection manager, alias reads, the activated-repository query hold and access check, the search-event recorder (`run_recorded_search`), the article sanitiser, the Web UI admin session/CSRF/elevation and golden-repos page | `_platform.server_platform()` (`ServerPlatformHost`, transitional) |
| content seam reads, path confinement, fetch batching | `_platform` (kernel slots and pure helpers) |

## Catalog access projection

The internal catalog (cidx-meta) is filtered per caller. The wiki never decides
what is restricted: every route asks `access_projection.catalog_view(viewer,
repo_dir, alias=..., owner=...)` and serves through the returned
`CatalogArticleView` (canonical served paths, projected article text, filtered
article lists and sidebar); `None` means nothing is restricted. Projected pages
are rendered per request and never written to the shared cache.
`wiki_article_analytics` filters its rows through `visible_article_views`.

The core implementation, `server/features/access_projection.py`, binds the
catalog projection: `server/services/cidx_meta_access/catalog_wiki.py` (the
same path policy, canonical-path rule and description/dependency-map
projection the file doors apply) for pages, assets, sidebar and search, and
`catalog_articles.visible_articles` over the catalog read path for analytics
rows. A restricted page is rendered per request and never cached. The guard-presence
test (`tests/unit/ci/test_catalog_guard_presence.py`) checks both ends: each
wiki route calls `restricted_catalog_view` / `_user_catalog_view`, and the core
capability calls the projection.

## Configuration

`ServerConfig.wiki_config` is gone. On load,
`config_manager._migrate_feature_owned_keys` moves a stored `wiki_config`
(runtime database row) into `feature_settings["wiki"]`; a value already in the
section wins.

## Migration 1

| Database | What version 1 does |
|---|---|
| PostgreSQL, core `024_wiki_article_views.sql` applied | no DDL; the runner records version 1 |
| PostgreSQL, 024 not applied | creates `wiki_article_views` (the 024 SQL) |
| SQLite (solo) | creates the three tables in the server database (`IF NOT EXISTS`) |

Core 001 and 024 stay in the core: 001 is the whole initial schema, and the
core's SQLite-to-PostgreSQL data migration copies the wiki tables.

## Import contracts

| Contract | Baseline |
|---|---|
| `core-does-not-import-wiki-feature` | 0 |
| `wiki-feature-no-core` | 3, all in `cidx_wiki._platform` (`kernel.capabilities`, `utils.path_confinement`, `indexing.fetch_planner`) |
| `wiki-feature-no-other-feature`, `xray-feature-no-other-feature` | 0 |

## Tests

```bash
PYTHONPATH=src:packages/cidx-sdk/src:packages/features/cidx-wiki/src:packages/features/cidx-xray/src:packages/backends/cidx-backend-workspace/src:packages/backends/cidx-backend-git/src:packages/cidx-testkit/src \
  pytest packages/features/cidx-wiki/tests tests/unit/server/wiki tests/unit/server/features/test_wiki_removability.py
```

## Not done yet

- The wiki cache storage backends still live in the core's backend registry;
  they move into the feature over the `storage` capability.
- The `wiki_url` search-result enrichment and the wiki controls in the core's
  repository templates name the wiki (gated by `feature_offered("wiki")`); a
  contributed result enricher and page fragment would remove them.
