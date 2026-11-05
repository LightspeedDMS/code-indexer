# Modular packaging: cidx core plus pip-installable add-ons

Status: proposal (not started). Written after the review of !256 (upstream v12.93.0 snapshot).
The target architecture is described in [modular-endstate.md](modular-endstate.md); this document holds the
measurements, seam inventory, risks and migration phases.

## Goal

`pip install code-indexer` gives the full local stack (a meta-package over `cidx-core`, the tool modules and the local
backends); a delegating pod installs `cidx-core` plus delegates instead. See modular-endstate.md. Features (X-Ray, SCIP, wiki, memory, dependency map) and
backends (content, git, ripgrep, index-store, scip-delegated, clone) are separate distributions that register
themselves on install. A deployment gets exactly the add-ons it installs.

## Does "pip install core, then add-ons" make sense?

Yes, with one rule: **add-ons depend on a tiny interface package, never on the core.** The repo already works
this way for two plugins. `plugins/cidx_git_backend/pyproject.toml` has `dependencies = []` and registers on the
`code_indexer.git_backend` entry-point group; `cidx_orchestrator_invoker` does the same for `code_indexer.cli_invoker`;
`cidx-content-backend` is installed from the private index in the Dockerfile. They work because the contract is a
factory function, with no imports of cidx internals.

A feature add-on that does `from code_indexer.server.services.file_service import ...` would make the core and the
add-on depend on each other, and the split would be cosmetic. So:

| Distribution | Contains | Depends on |
|---|---|---|
| `cidx-sdk` | Protocols, DTOs, entry-point group names, the feature registration API, a version constant | nothing |
| `cidx-core` (the kernel) | CLI, server, storage, auth, config, MCP protocol, tool registry, kernel services. No local seam implementations | `cidx-sdk` |
| `code-indexer` (meta-package) | No code. The default full local stack: depends on `cidx-core`, the tool modules and the local backends. Forwards the `server` and `cluster` extras to `cidx-core` | `cidx-core`, tool modules, `cidx-backend-*` |
| `cidx-xray`, `cidx-scip`, `cidx-wiki`, `cidx-memory`, `cidx-depmap` | feature handlers, services, routes, tool docs, migrations, UI pages | `cidx-sdk` |
| `cidx-delegate-workspace`, `-git`, `-sandbox`, `-orchestrator` | delegate plugins, one distribution per remote service (see Seams) | `cidx-sdk` |
| `cidx-backend-workspace`, `cidx-backend-git`, `cidx-clone-ontap`, `cidx-clone-cow` | local implementations of seams, optional | `cidx-sdk` |

Extras on `cidx-core` pull individual add-ons in: `cidx-core[xray,scip,wiki]`. The extras only list
distributions; discovery is by entry point, so installing an add-on directly behaves the same way.

Alternatives considered:

- **Extras only, single distribution.** Cheapest, but it only changes dependency installation, not code ownership.
  It does not reduce merge conflicts or let a backend ship on its own schedule. Not recommended as the end state.
- **Separate services (microservices).** Needs a network API for everything that is an in-process call today
  (the web UI alone makes 128 imports into `server.services`). Not recommended.

## Feature registration contract (in `cidx-sdk`)

A feature add-on exposes one entry point in group `code_indexer.features` returning a `Feature` object:

| Hook | Purpose |
|---|---|
| `tools()` | MCP tool definitions and handlers (replaces hand-wiring through `handlers/_legacy.py`) |
| `routers()` | FastAPI routers for REST and web pages |
| `on_startup(ctx)` / `on_shutdown(ctx)` | replaces the feature's slice of `startup/lifespan.py` (7,001 lines today) |
| `config_section()` | config schema and defaults for the feature |
| `migrations()` | DB migrations, ordered per feature |
| `requires` | kernel capabilities and SDK version range, checked at load |

The core loads features at startup, refuses a feature whose SDK range does not match, and lists loaded features in
the health endpoint so a missing add-on is visible instead of a silent 404.

## Decisions taken

- We own upstream, so upstream-owned code may be split. The fork/upstream distinction no longer limits scope;
  the snapshot procedure is replaced by ordinary releases of one monorepo.
- One monorepo, several distributions (uv workspace or equivalent). SDK changes stay atomic with their consumers.
- A feature is on when it is installed AND named in `enabled_features` (bootstrap key). Enabled but not installed
  stops startup (fail loud); installed but not enabled is never imported and is listed in health.
- In the monorepo the core depends on the SDK (and add-ons on the SDK) by path; publishing to a registry comes later.
- No built-in seam fallback: a seam configured with no module implementing it stops startup, naming the seam.
- One entry-point group per seam; local and delegated implementations register in the same group and are chosen by
  entry-point name.
- A feature health report that is not healthy makes the overall status DEGRADED, never UNHEALTHY.
- Features are server-side. CLI-only builds use the kernel commands; feature CLI commands appear only where the
  server's `config.json` enables an installed feature.

## Seams: inventory and packaging

"Workspace" is not itself a seam. It is a remote service (workspace-mcp) that several seams delegate to: content,
content writes, ripgrep, clone, and index artifacts. Git delegates to git-mcp. X-Ray and SCIP delegate to sandbox-mcp.
The seams are the config selectors below. Counts are files under `src/code_indexer` mentioning the keyword (grep,
approximate).

| Seam (config key) | Entry-point group | Protocol today | Resolver | Files |
|---|---|---|---|---|
| `content_backend` | `code_indexer.content_backend` | `ContentBackend` (`storage/shared/content_backend.py`) | shared | 87 |
| content writes | `code_indexer.content_writer` | none found (consumer is `ContentWriterSink`) | shared | 9 |
| `git_backend` | `code_indexer.git_backend` | `GitBackend`, `GitCommandRunner` (`storage/shared/git_backend.py`) | shared | 69 |
| git invocation | `code_indexer.git_invoker` | `GitInvoker` (`utils/git_invoker.py`) | own loader | 2 |
| `ripgrep_backend` | `code_indexer.ripgrep_backend` | `RipgrepBackend` (`storage/shared/ripgrep_backend.py`) | shared | 17 |
| `search_backend` | `code_indexer.search_backend` | `SearchBackend` (inside `startup/search_backend_wiring.py`) | shared | 11 |
| `index_store_backend` | `code_indexer.index_artifacts` | `IndexStore` (`utils/index_store.py`) | shared | 75 |
| `scip_backend` | `code_indexer.scip_backend` | none found (`ScipBackendFactory` class) | shared | 19 |
| `xray_backend` | `code_indexer.xray_backend` | none found | shared | 10 |
| `clone_backend` (local, ontap, cow-daemon, delegated) | none | `CloneBackend` (`storage/shared/clone_backend.py`) | config switch in `clone_backend_wiring.py` | 24 |
| CLI invoker | `code_indexer.cli_invoker` | factory contract | own loader | 16 |
| control-plane metadata | none (`storage_mode`) | none | `control_plane_metadata_wiring.py` | 20 |

Correction to the earlier draft: Protocols already exist for content, git, ripgrep, search, index store and clone, and
eight seams already share one resolver (`server/services/backend_plugin_loader.py`, `BackendPluginResolver`). What is
missing is Protocols for content writes, SCIP and X-Ray, a plugin group for clone, and moving all of it into the SDK.

### What changes for seams

1. **Move every seam Protocol and the resolver into `cidx-sdk`.** They are scattered today across `storage/shared`,
   `utils` and `startup`. The two own-loader seams (git invoker, CLI invoker) move onto the shared resolver.
2. **Make local an implementation of the same Protocol, not a branch in the caller.** Today consumers branch on
   local versus delegated (`content_seam_source`, `delegated_refusal`, per-route `if delegated` code). That branching
   is where the !256 Critical came from: the local path kept the access projection and the delegated path lost it.
   Features should call one Protocol; the local and delegated implementations are interchangeable behind it.
3. **One conformance suite per seam, run against every implementation.** The git-seam AST guard, the content seam
   inventory and the ripgrep Pact test (`tests/contracts/workspace-mcp/`) already point this way. They move into
   a shared `cidx-sdk` test kit that each backend distribution runs in its own CI.
4. **Local implementations also move out of the kernel**, into optional `cidx-backend-*` modules. A delegating pod
   (EKS) installs the `cidx-delegate-*` plugins instead and carries no local code for those seams. Fail-loud stays:
   a seam with neither module installed aborts startup. The one-command workstation install is the `code-indexer`
   meta-package, which depends on `cidx-core`, the tool modules and the local backends. This supersedes the earlier "local stays in the core" draft; see
   [modular-endstate.md](modular-endstate.md) sections 1 and 7.
5. **Group delegated implementations by the remote service they talk to**, since each group shares transport, auth
   and endpoint config:

| Distribution | Seams | Remote service |
|---|---|---|
| `cidx-delegate-workspace` | content, content writer, ripgrep, clone (delegated), index artifacts | workspace-mcp |
| `cidx-delegate-git` | git backend, git invoker | git-mcp (today `plugins/cidx_git_backend`) |
| `cidx-delegate-sandbox` | X-Ray (incl. inline), SCIP | sandbox-mcp |
| `cidx-delegate-orchestrator` | CLI invoker | orchestrator (today `plugins/cidx_orchestrator_invoker`) |
| `cidx-clone-ontap`, `cidx-clone-cow` | ONTAP FlexClone and CoW-daemon clone | storage / daemon; need a new `code_indexer.clone_backend` group |

Today `cidx-content-backend` bundles content, X-Ray and SCIP (since 0.7.0 and 0.8.0) behind one version, built in
the evo-sandbox repo and installed from a private registry. Splitting by service lets each move on its own cadence.
It also means that package's source has to come into the monorepo, or stay external behind the same SDK contract
(open question 3).

### Phase 5 as built (branches `feature/modular-5-seams`, `feature/modular-5b-seams`)

Every Protocol, DTO and error type in the table above now lives in `cidx_sdk.seams.<seam>` (SDK 0.4.0); the old
paths re-export. All seams resolve through the SDK's `SeamResolver`: the env var for a delegate mode, then the ONE
entry point in the seam's group whose NAME is the configured mode, else `SeamUnavailableError` naming the seam.
Delegate wheels published under other names (`workspacemcp`, `gitmcp`, `sandbox`, `orchestrator`) still satisfy
`delegated`. Local implementations live in `cidx-backend-workspace` and `cidx-backend-git`; the core reaches them
only through the resolver and hands a backend the core services it needs as factory arguments typed by SDK
Protocols. Detail: [docs/architecture/seams.md](../../docs/architecture/seams.md).

| Seam | Protocol now in | `local` reached through the resolver | Conformance (`cidx-testkit`) | Left to do |
|---|---|---|---|---|
| content | `cidx_sdk.seams.content` (+ `content_identity`) | yes: `cidx-backend-workspace` | local, fake workspace-mcp, both modes as resolved | caller branching (below); delegate distribution |
| content writer | `seams.content_writer` | yes: `cidx-backend-workspace` (`LocalContentWriter`) | local, fake workspace-mcp, both as resolved | callers still write the disk under local; switch them with the content caller step (after !256) |
| git | `seams.git` (+ `GitProcessRunner`) | yes: `cidx-backend-git`, given the core's git runner | local, fake git-mcp, both as resolved | delegate distribution (`cidx-delegate-git` from `plugins/cidx_git_backend`) |
| git invoker | `seams.git_invoker` | n/a (no invoker = native git) | fake git-mcp, as resolved | delegate distribution |
| ripgrep | `seams.ripgrep` (+ `BoundedProcessRunner`) | yes: `cidx-backend-workspace`, given the core's bounded runner | fake; local and delegated as resolved; Pact unchanged | move the Pact with `cidx-delegate-workspace` |
| search | `seams.search` | n/a (local = the query engine) | none: no delegate or fake | suite when a delegate exists |
| index store | `seams.index_store` (+ `index_root_for`) | yes: `cidx-backend-workspace` (`LocalIndexStore`) | local; core `DelegatedIndexStore` over the fake as resolved | `DelegatedIndexStore` moves with the delegate |
| SCIP | `seams.scip` | n/a (local indexers in `SCIPGenerator`, the future `cidx-scip`) | fake sandbox-mcp, two replicas sharing the result store; as resolved | local engine moves with `cidx-scip` |
| X-Ray | `seams.xray` | n/a (engine is `cidx_xray`'s) | fake sandbox-mcp, raw, as `cidx_xray.server` wires it, and inline | local engine run (needs xray-cli and rustc) |
| clone | `seams.clone` (+ `CloneBackendSettings`, `CloneServices`) | yes: `local`, `ontap`, `cow-daemon` in `cidx-backend-workspace`; `delegated` registered by the core | local; core delegated client over the fake; fake as resolved; ontap/cow-daemon resolution only | `cidx-clone-ontap` / `cidx-clone-cow` split deferred (seams.md); delegated client to `cidx-delegate-workspace` |
| CLI invoker | `seams.cli_invoker` (+ `InvocationResult`, `REPOSITORY_SOURCE`) | n/a (no plugin = built-in CLI) | orchestrator delegate vs a fake orchestrator, in its plugin and as resolved | delegate distribution |

Import contracts: `backends-no-kernel` and `plugins-no-kernel` are at zero (the orchestrator invoker's three core
imports went: its types come from the SDK and the core's repository source from an SDK slot);
`core-does-not-import-backends` is new, with the six compatibility re-exports as its baseline.

Open question 5 below (ONTAP and CoW-daemon packaging): kept in `cidx-backend-workspace` for now; see seams.md.

Static guards (git seam, content-seam inventory, index-store ratchet) run on engines in `cidx_testkit.guards` over a
source tree that spans the in-repo distributions, so `LocalContentBackend` in `cidx_backend_workspace` is still
inventoried. The ripgrep Pact stays in `tests/` until `cidx-delegate-workspace` exists.

### Content seam: where the projection attaches

Done on `feature/modular-5-content-callers`: `content_seam_source` always returns a source (local included), the
choice of reader is the implementation's (`pod_directory`), and writes go through `content_writer_for(source)`.
Gitignore, exclusions, `indexed_only` and the projection stay above the seam in `FileListingService` for both
readers; the local readers keep link-following and index fast paths the Protocol lacks. Branches left (git
placement, delegated-only refusals, lifecycle authority of this pod's disk) and why:
[seams.md](../../docs/architecture/seams.md#branches-that-stay-and-why). The catalog access projection (MR !256, `server/services/cidx_meta_access/`)
must attach ABOVE the seam, once. The call sites it uses on !256, grouped by the seam-facing read they guard:

| Projection module (!256) | Call sites above the seam |
|---|---|
| `file_doors`, `tree_projection`, `catalog_requests` | `server/mcp/handlers/files.py`, `server/routers/files.py`, `server/routers/inline_repos.py`, `server/routers/inline_repos_v2.py`, `server/managers/composite_file_listing.py` |
| `catalog_search`, `catalog_regex`, `catalog_fulltext`, `request_activations` | `server/mcp/handlers/search/regex_search.py`, `search/omni.py`, `server/routers/inline_query.py`, `server/routers/inline_jobs.py`, `server/query/semantic_query_manager.py` |
| `cached_handles` | `server/mcp/handlers/search/cached_content.py`, `search/code_search.py`, `server/routers/cache.py`, `server/mcp/handlers/xray/_cached_payload.py` |
| `history_policy` | `server/mcp/handlers/_catalog_history.py`, `search/temporal_search.py`, `xray/_catalog_guard.py`, `server/routers/repo_access_http.py` |
| `depmap_source`, `universe_provider` | `server/mcp/handlers/depmap.py`, `files.py`, `regex_search.py` |
| `catalog_articles` | `server/mcp/handlers/guides.py` |
| `acting_scope` | `server/mcp/protocol.py` |

Every file read among these goes through `FileListingService` (`list_files`, `list_files_by_path`,
`list_directory_level`, `get_file_content`, `get_file_content_by_path`), which is where the local/seam branch is
today. Once that branch moves into the backends, those five methods (plus the search-hit and cached-handle
reprojections, which never read files) are the one place the projection wraps, identically for both modes;
acceptance criterion 6's non-admin delegated test runs against the fake workspace-mcp.

### Seam risks

- 87 files touch the content seam and 69 the git seam, so moving the delegated branches behind a Protocol is a wide
  change. Do it seam by seam, content first, because it is the one with the most consumers and the known parity gap.
- The `workspace-mcp` Pact contract has to be deployed in order (workspace-mcp first, then cidx). Keep that rule in
  the release process when `cidx-delegate-workspace` ships separately.
- Control-plane metadata is driven by `storage_mode`, not a plugin seam. Leave it in the core unless there is a
  reason to make it swappable.

## Current state (measured, static AST; names matched heuristically)

- `server/` is 365k of about 520k LOC. `server/services` is 139k.
- Import-time cycles are almost absent (3, each two modules), but 1,368 function-level imports hide the real ones.
- Library packages import the server: `global_repos` 65 imports of `server.services`, `storage` 13, `xray` 14, `scip` 7.
- X-Ray: 41 modules, 35 external dependencies (server startup wiring, `file_service`, telemetry, `config_service`).
  Only 7 modules import it. SCIP: 49 modules, 45 external dependencies, 10 importers.
- `web/routes.py` is 13,368 lines; `auth` and `routers` import `web`, which imports them back.
- `tests/unit/server` has 2,858 test files. Tests patch `handlers.X` bindings, so moving a handler breaks mocks.

## Risks

1. **Retiring the snapshot procedure.** The repo currently takes upstream snapshots
   (`docs/upstream-snapshot-merge.md`); !256 had 26 conflicts and silently lost catalog access-control guards in
   `file_service.py`. Since upstream is ours, the split can proceed everywhere, but the two histories must be
   reconciled first: land or abandon any in-flight snapshot (!256) before moving files, then stop snapshotting and
   treat the monorepo as the single source. The seam conformance suites (see Seams) replace the manual guard checks
   a snapshot needed.
2. **Version skew.** Core and add-ons release independently. Mitigation: SDK semver, `requires` check at load,
   and a CI matrix of the last two SDK minors.
3. **Silent absence.** A pod without an add-on must fail loud at startup when config enables that feature.
4. **Packaging surface.** `pyproject.toml` (single `packages = ["src/code_indexer"]`), `setup.py`,
   `pyinstaller.spec`, the Dockerfile and the private index publishing all change.

## Phases

| # | Work | Exit criterion | Size (estimate, not measured) |
|---|---|---|---|
| 0 | Scope decided: we own upstream, one monorepo. Remaining prerequisite: land or abandon !256 and retire the snapshot procedure | no open snapshot MR; process doc updated | 1 day |
| 1 | import-linter contracts in CI with a ratchet (model: `index_store_ratchet.json`), plus contract tests that fail when a seam guard (`served_text`, `lines_view`, `restricted_catalog_view`) disappears | CI red on a new core-to-server import and on a deleted guard | days |
| 2 | Kernel extraction: move seam types and `iter_manifest_entries` / `ContentSeamSource` out of `file_service.py`; config and telemetry access behind interfaces; re-point X-Ray and SCIP | `xray` and `scip` import no `server.*` | 1-2 weeks |
| 3 | `cidx-sdk` plus the `Feature` contract; core loads features by entry point; keep `handlers.X` re-exports so test patches survive | one in-tree feature loads through the contract | 1-2 weeks |
| 4 | Move X-Ray (best first candidate: few importers, Rust crate already separate) into `cidx-xray`; then SCIP, wiki, memory, dep map | each is installable and removable; core tests pass without it | weeks per feature |

Phase 4b status (X-Ray): the engine, the evaluator cache store and the `cidx xray` CLI group are in
`packages/features/cidx-xray` (`cidx_xray`), registered as the `xray` feature. The MCP handlers, REST routes, web
config section, execution-backend wiring and lifespan wiring stay in the core, behind `core_wires("xray")`, until the
core consumes the feature hooks (4a) and the capabilities they need exist. X-Ray is not yet removable: the core
depends on `cidx-xray`. Detail: [docs/architecture/cidx-xray.md](../../docs/architecture/cidx-xray.md).
| 5 | Seams (see Seams section): move Protocols and resolver into `cidx-sdk`; add missing Protocols (content writer, SCIP, X-Ray) and a clone plugin group; put local behind the same Protocol; ship one conformance suite per seam; split delegated implementations into the per-service distributions. Content seam first | each seam: one Protocol, local and delegated both pass the same suite, selected by config and installed by pip | days to 2 weeks per seam |
| 6 | Split `web/routes.py` into per-feature routers shipped by the feature add-ons. A separate UI process only after an API covers the in-process calls | `routes.py` below an agreed size; UI pages come from add-ons | weeks, open-ended |

Phases 1 and 2 are worth doing under any answer to scope. Phases 3-6 depend on Phase 0.

## Open questions

Resolved: upstream-owned code may be split (we own upstream); one monorepo.

1. Is !256 merged or abandoned before the first file moves?
2. Do the content, X-Ray and SCIP backends currently built in the evo-sandbox repo (`cidx-content-backend`) move into
   this monorepo, or stay there and only conform to `cidx-sdk`? The first gives atomic changes; the second keeps
   the workspace-mcp / sandbox-mcp implementations next to the services they call.
3. Where do add-ons publish: the private registry used for `cidx-content-backend` today?
4. Do pyinstaller and the CLI-only install need features at all, or only the server image?
5. ONTAP and CoW-daemon clone backends: separate add-ons or one? The group exists and both are out of the core
   (`cidx-backend-workspace`); splitting them into `cidx-clone-ontap` / `cidx-clone-cow` is deferred until a
   deployment needs one without the other.

## Not verified

No code was moved to test any of this. Module counts come from name matching and may include or miss files.
Phase sizes are judgment, not measurements.
