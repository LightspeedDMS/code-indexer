# cidx modular end state

Status: design target. Describes where the code should end up, not how to get there. The migration path, measurements
and open questions are in [modular-packaging-addons.md](modular-packaging-addons.md). Figures quoted from the current
code come from that document; everything under "target" is a proposal, not a fact about today's code.

## 1. What "done" looks like

One monorepo that builds a small core, a small SDK, and a set of independently installable add-ons. A deployment is a
core plus the add-ons it lists. Nothing in the core knows a feature exists, and no feature reaches into the server's
internals.

| Property | Target |
|---|---|
| `pip install code-indexer` | The full local stack. A thin meta-package with no code that depends on `cidx-core`, the tool modules and the local backends. This is the default for users and workstations |
| `pip install cidx-core` | The kernel: platform runtime, CLI and server shells, query engine. Pulls in `cidx-sdk`. No local backends and no tool modules. This is what a delegating pod starts from |
| `pip install code-indexer[server,cluster]` or `cidx-core[server,cluster]` | Adds the server and PostgreSQL mode. The meta-package forwards these extras to `cidx-core` |
| `pip install cidx-xray` (or `cidx-fts`, `cidx-scip`, an extra) | That tool's handlers, routes, UI pages, CLI commands and migrations appear; removing it removes them cleanly |
| `pip install cidx-backend-workspace` (or `-git`) | The local implementation of those seams (local filesystem, clone, ripgrep, in-process git). Optional |
| `pip install cidx-delegate-workspace` (or `-git`, `-sandbox`) | The plugin that points the seam at the remote service. Optional |
| A seam with neither a local module nor a delegate installed | Startup aborts, naming the seam. There is no silent fallback |
| Why two names | Extras can only add packages, never remove them. A default that bundles everything, plus a lean kernel for pods, needs two distributions: `code-indexer` (meta) and `cidx-core` (kernel) |
| Core imports an add-on | Never (CI-enforced) |
| Add-on imports core internals | Never. It imports `cidx-sdk` only (CI-enforced) |
| Add-on imports another add-on | Never. Shared needs become SDK capabilities |
| Library packages (`global_repos`, `storage`, `xray`, `scip`) importing `server.*` | Zero |
| Local and delegated behavior of a seam | Same Protocol, same conformance suite, both pass |

## 2. Repository layout

```
packages/
  cidx-sdk/                 interfaces only; no runtime dependencies
  cidx-core/                kernel: platform runtime, CLI shell, server shell, UI shell, query engine
  code-indexer/             meta-package, no code: depends on cidx-core, the tool modules and the local backends
  features/
    cidx-xray/              handlers, services, routes, UI pages, tool docs, migrations, rust/xray-core build
    cidx-scip/
    cidx-wiki/
    cidx-memory/
    cidx-depmap/
    ...                     any other self-contained tool family, by the same rule
  backends/                 LOCAL implementations of seams (optional)
    cidx-backend-workspace/     local filesystem content, content writer, ripgrep, clone, index store
    cidx-backend-git/           in-process git
    cidx-clone-ontap/
    cidx-clone-cow/
  delegates/                plugins that point a seam at a remote service (optional)
    cidx-delegate-workspace/    workspace-mcp: content, content writer, ripgrep, clone, index artifacts
    cidx-delegate-git/          git-mcp: git backend, git invoker
    cidx-delegate-sandbox/      sandbox-mcp: X-Ray, SCIP
    cidx-delegate-orchestrator/ CLI invoker
  cidx-testkit/             seam conformance suites, fixtures, fake services (dev dependency only)
docs/  plans/  deployment/  scripts/
```

All distributions live in one workspace with one lockfile. SDK and core change together in one commit when an
interface changes; add-ons that break fail CI in the same pull request.

## 3. Dependency rules

```
cidx-sdk  <--  cidx-core (the kernel)
cidx-sdk  <--  every feature
cidx-sdk  <--  every backend
```

- Arrows are the only allowed imports. Features, backends and delegates never import `cidx-core` (the `code_indexer` package), each other, or `server.*`.
- The core discovers add-ons only through entry points (`code_indexer.features`, `code_indexer.<seam>`).
- Enforced by import-linter contracts in CI, plus a ratchet file so the count of violations can only go down during
  migration and must be zero at the end.
- Function-level imports count. Today 1,368 imports are inside functions and hide cycles; the contracts scan them.

## 4. The SDK (`cidx-sdk`)

Pure interfaces, versioned with semver, no third-party dependencies.

| Contents | Examples |
|---|---|
| Seam Protocols and error types | content, content writer, git, git invoker, ripgrep, search, index store, scip, xray, clone, CLI invoker |
| Plugin resolver | env var, then entry point; if neither resolves, fail loud naming the seam. There is no built-in default, because the kernel holds no local implementation |
| Feature contract | the `Feature` object and its hooks (section 6) |
| Capability interfaces | what the core offers a feature (section 5) |
| DTOs | result and request shapes shared across a seam |
| Constants | entry-point group names, config key names, `SDK_VERSION` |

Anything an add-on or the testkit needs from the core is promoted into the SDK deliberately, with a version bump.

## 5. The kernel (`cidx-core`)

Owns the product's non-negotiable behavior and the platform every feature runs on:

- Query: semantic, FTS and regex search, indexing, chunk storage, temporal, the embedding providers. ("Query is
  everything" stays in the core and is never an add-on.)
- Repositories: golden and activated repos, aliases, versioned snapshots, refresh scheduler, cidx-meta.
- Platform services exposed to features as capabilities: configuration, auth and access filtering, permissions,
  background jobs, payload cache, search-event recording, telemetry, HTTP client factory, storage backends
  (SQLite and PostgreSQL), health, audit log.
- Shells: CLI entry point, FastAPI app, MCP protocol and tool registry, web UI layout, navigation and static assets.
- No local seam implementations. Those live in optional `cidx-backend-*` modules (section 7), so a pod that
  delegates a seam does not carry the local code for it.

The core exposes capabilities through a single `FeatureContext` handed to a feature at startup. A feature asks the
context for what it needs and receives an SDK-typed object; it never imports a core module to get one.

## 6. Feature contract

A feature add-on exposes one entry point in group `code_indexer.features` returning a `Feature`:

| Hook | What the feature contributes |
|---|---|
| `manifest` | name, version, `requires_sdk` range, required capabilities, optional dependencies on a backend seam |
| `tools()` | MCP tools: handler plus the tool-doc markdown, packaged in the wheel |
| `routers()` | REST routers |
| `ui_pages()` | web pages: routes, templates, static files, navigation entries, required permission |
| `cli_commands()` | subcommands under the CLI (today `cli_xray` and `cli_scip`) |
| `config_section()` | typed config schema, defaults, which keys are bootstrap and which are runtime |
| `migrations()` | ordered, backward-compatible DB migrations, tracked per feature |
| `health()` | health contributors and metrics |
| `on_startup(ctx)` / `on_shutdown(ctx)` | wiring that lives in the 7,000-line `lifespan.py` today |
| `background_jobs()` | jobs registered with the job manager, so they appear in the dashboard |

Loading rules: the core loads features in dependency order, checks `requires_sdk`, and refuses to start if a feature
is configured on but not installed. The health endpoint lists every loaded feature and version, so an absent add-on
shows up as a named gap instead of a missing route.

## 7. Seams

Every seam has exactly one SDK Protocol and one conformance suite in `cidx-testkit`.

- Each seam is satisfied by exactly one of two kinds of module: a local module (`cidx-backend-*`) or a delegate
  plugin (`cidx-delegate-*`) that calls a remote service. The kernel holds neither. If a seam is configured
  `local` and no local module is installed, or `delegated` and no delegate is installed, startup aborts.
- The local engines of X-Ray and SCIP are part of `cidx-xray` and `cidx-scip` themselves, since they are the tools.
  Only their delegate (`cidx-delegate-sandbox`) is separate.
- Callers hold the Protocol. No caller branches on "local or delegated"; the branching that exists today
  (`content_seam_source`, `delegated_refusal`, per-route checks) moves inside the implementations. A security
  projection such as the catalog access filter is applied by the layer above the seam, once, so no implementation can
  skip it.
- Conformance: the same suite runs against the local implementation and against each delegated one (using the
  testkit's fake of the remote service). Contract tests against the real remote service (the ripgrep Pact for
  workspace-mcp) live in the backend distribution that owns the call.
- Selection stays config-driven (`content_backend`, `git_backend`, `ripgrep_backend`, `search_backend`,
  `index_store_backend`, `scip_backend`, `xray_backend`, `clone_backend`). There is no silent fallback: a delegated
  choice with no implementation fails startup.
- One delegate distribution per remote service (workspace-mcp, git-mcp, sandbox-mcp, orchestrator), so transport,
  auth and endpoint configuration live together.
- Mixed deployments are allowed: each seam is chosen independently. A pod may delegate content and git and still
  keep a local module for a seam it runs in-process (for example search over pod-local indexes; which seams EKS
  actually runs locally has not been verified).

## 8. User interface

- The core owns the web shell: authentication, session, CSRF, layout, navigation, theming, static assets and the
  admin pages that belong to the platform (users, groups, audit, config, jobs, health).
- Feature pages are contributed by the feature (`ui_pages()`), rendered inside the shell, and gated by the same
  permission model as the feature's tools.
- Pages call application services through `FeatureContext` capabilities, never through imports of server modules.
  The current 13,368-line `web/routes.py` ends as many small per-feature route modules.
- The shell and platform pages do not import features. A feature page appears in navigation only when its add-on is
  installed and enabled.
- Out of scope for the end state: running the UI as a separate process or SPA. That requires an HTTP API covering
  every in-process call the pages make today and is a separate decision.

## 9. Data and migrations

- Storage backends (SQLite solo, PostgreSQL cluster) and their Protocols stay in the core. Features ask for a store
  through the context.
- Each distribution owns its migrations and a per-distribution version row. Order across distributions is resolved by
  declared dependencies; the migration runner keeps the PostgreSQL session advisory lock.
- The existing rule is unchanged: migrations are backward compatible for rolling restarts (add tables and nullable
  columns; never drop, rename or retype).
- Removing a feature never drops its tables. The tables stay, inert.
- Cluster-aware state rules apply to every add-on: no per-node dicts for cross-request state; use the payload cache,
  job tracker or shared store from the context.

## 10. Configuration

- Each feature registers a config section schema. The Web UI config screen is assembled from the registered schemas,
  so adding a feature adds its settings page without editing the core.
- The bootstrap versus runtime split is preserved: bootstrap keys stay in `config.json`, runtime settings in the
  database via the config service. A feature declares which of its keys are which.
- No new environment variables for server settings, as today. Backend add-ons may read their own endpoint and token
  from the environment, as the existing plugins do.

## 11. Versioning, compatibility, release

| Item | Rule (proposal) |
|---|---|
| `cidx-sdk` | semver; a breaking change is a major bump and requires every in-repo add-on to move in the same pull request |
| Core and in-repo features | one release train; the core lists the feature versions it was tested with |
| Backends | independent versions; each declares the SDK range it supports |
| Load-time check | core refuses an add-on whose `requires_sdk` range excludes the running SDK |
| Support window | CI tests the current SDK and the previous minor against the add-ons |

## 12. Deployment profiles

The server image is built from a profile, a named list of add-ons:

| Profile | Contents |
|---|---|
| standalone | `pip install code-indexer`: kernel, tool modules, local modules (`cidx-backend-*`); no delegates. Workstation and solo server |
| eks-delegated | `pip install cidx-core` plus the tool modules it wants, `cidx-delegate-workspace`, `-git`, `-sandbox`; no `code-indexer` meta-package, so no local modules for the seams it delegates |
| full | everything, for CI and end-to-end tests |

A pod's profile must contain a module for every seam it configures; the image build asserts the entry points
resolve, as the current Dockerfile step does for `cidx-content-backend`. The CLI-only install uses the kernel without
the server extra.

## 13. Testing

- Each distribution has its own test tree and runs in its own CI job. A change to one add-on does not run another
  add-on's tests unless they share the SDK.
- The core's tests run with no add-ons installed (the minimal profile). That is what proves the core does not
  depend on a feature.
- Add-on tests run against core plus SDK.
- The testkit conformance suites run per seam per implementation.
- The existing gates map onto packages: the fast and server-fast suites run per package; the Rust suite belongs to
  the distribution that ships `rust/xray-core`; the end-to-end suite runs once against the full profile through the
  REST and MCP front door.
- Static guards that exist today (git-seam AST check, content-seam inventory, index-store ratchet) become part of the
  testkit so any implementation or feature is checked against them.

## 14. Invariants carried over unchanged

- Query remains the core product and stays in the core.
- Fail loud, no silent fallback for a selected delegated backend.
- No synchronous filesystem or network call inside `async def`; O(fleet) work is offloaded and paced (design for
  roughly 900 repositories).
- Cluster-aware state only; no per-node memory for cross-request state.
- Backward-compatible migrations.
- Config: bootstrap versus runtime split; no environment variables for server settings.
- Authorization projection is applied above the seam, once.

## 15. Acceptance criteria

The end state is reached when all of these hold:

1. Import-linter contracts pass with zero baseline violations, including function-level imports.
2. The core test suite passes with no add-on installed; each add-on's tests pass against core plus SDK only.
3. Uninstalling any single feature leaves the server starting and every other feature working; the removed
   feature's routes, tools, pages and CLI commands are gone.
4. Every seam has one Protocol and a conformance suite that both its local and delegated implementations pass.
5. `lifespan.py` and `web/routes.py` contain no feature-specific code.
6. A catalog-style access projection exists in exactly one place per seam, with a non-admin test in delegated mode.
7. A feature's tool docs, migrations, config schema and UI pages ship inside its own wheel.

## 16. Not decided

- ~~Whether the Rust X-Ray engine ships as platform wheels or is built in the image.~~ Decided in phase 4b: built
  in the image (the Dockerfile `xray-builder` stage) and in source checkouts, from `rust/` at the repository root.
  `rust/` belongs to cidx-xray but stays where it is until X-Ray has its own CI job; the `cidx-xray` wheel is pure
  Python and finds `xray-cli` through `CIDX_XRAY_CLI_PATH` or the source tree. Platform wheels would not remove the
  build step, because the engine also needs the pinned rustc to compile evaluators. See
  [docs/architecture/cidx-xray.md](../../docs/architecture/cidx-xray.md).
- Lockstep versioning for in-repo features versus fully independent versions.
- Import names. The kernel keeps the `code_indexer` import package; add-ons use their own top-level names
  (as `cidx_git_backend` does today) rather than sharing `code_indexer` as a namespace package. Existing
  `code_indexer.xray` / `code_indexer.scip` import paths would need thin compatibility shims during the move.
  Done for X-Ray in phase 4b: `cidx_xray`, with `code_indexer.xray.<module>` an import alias for the same module
  object.
- The Dockerfile, `setup.py` and `pyinstaller.spec` change: the image installs `cidx-core` and delegates instead
  of `pip install ".[cluster,cohere]"`.
- Whether `cidx-delegate-*` source comes into the monorepo or stays external behind the SDK contract.
- Whether semantic query stays in the kernel (the "Query is everything" invariant argues yes) or is also a module
  like `cidx-fts`. FTS already imports lazily, so making it optional is cheaper than semantic search.
- Whether repository lifecycle code that assumes a local filesystem (snapshot manager, versioned snapshots,
  refresh scheduler) belongs in `cidx-backend-workspace` or in the kernel. It decides how much of the core a
  delegating pod can leave out.
- Which other tool families (CI/CD, SSH keys, pull requests, dependency map variants) become features, by the
  same rule: self-contained, with their own tools, routes and storage.

## 17. Not verified

This is a design, not a measurement. No code was moved. The feature hooks and capability list are inferred from what
`lifespan.py`, the handler registry and the existing plugins do today, and have not been checked against every
feature. The profile names and release rules are proposals.
