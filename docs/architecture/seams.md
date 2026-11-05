# Seams as Built

Status: modular plan phase 5; every seam resolves through the SDK resolver, and
content callers hold a source for both modes (the caller-side content step). This page describes what the code
does today; the target is in `plans/designs/modular-endstate.md` section 7 and
the per-seam status in `plans/designs/modular-packaging-addons.md`.

A seam is a boundary where the work can happen here (a local implementation)
or in a remote service (a delegate). Each seam has one Protocol, one
entry-point group, and a configured MODE.

## Where things live

| Piece | Location |
|---|---|
| Seam Protocols, DTOs, error types | `cidx_sdk.seams.<seam>` (`packages/cidx-sdk`) |
| Content identity (the git blob hash) | `cidx_sdk.seams.content_identity` |
| Resolver (`SeamResolver`) and selection rules | `cidx_sdk.seams.resolver`, `cidx_sdk.seams.selection` |
| Entry-point group names | `cidx_sdk.entry_points.SEAM_ENTRY_POINT_GROUPS` |
| Reading the configured mode | the core (config service, `server/startup/*_wiring.py`) |
| Core adapter to the resolver | `code_indexer.kernel.seam_resolution`, `server/services/backend_plugin_loader.py` |
| Local workspace seams (content, content writer, ripgrep, index store, clone) | `cidx_backend_workspace` (`packages/backends/cidx-backend-workspace`) |
| Local git seam (in-process git) | `cidx_backend_git` (`packages/backends/cidx-backend-git`) |
| Conformance suites, fakes, static guard engines | `cidx_testkit` (`packages/cidx-testkit`, dev only) |

Old import paths (`code_indexer.kernel.seam_types`,
`server/storage/shared/*_backend.py`, `utils/index_store.py`,
`utils/git_invoker.py`, `scip/delegation_api.py`, the `*_plugin.py` error
classes, `intelligence_cli_invoker.py`) re-export the SDK names and the moved
local classes, so imports, `isinstance` checks and test patches see the same
objects. The core never CONSTRUCTS a local class: every default goes through
the resolver (`local_git_backend()`, `local_ripgrep_backend()`,
`ContentBackendFactory.create("local")`, `clone_backend_plugin.resolve_clone_backend`,
`kernel.index_store_seam`). The re-exports are the whole baseline of the
`core-does-not-import-backends` import contract.

## What a local backend is handed

A backend imports `cidx_sdk` only (`backends-no-kernel`, zero violations).
Where the local implementation needs a core runtime service, the core passes
it to the factory (`SeamResolver.resolve(mode, *args)`), typed by an SDK
Protocol; the core's adapter looks its helper up at call time, so test patches
of the old core module still apply. Pure helpers moved into the SDK instead.

| Seam | Factory argument (SDK type) | Core adapter |
|---|---|---|
| git | `GitProcessRunner` (`seams.git`) | `CoreGitProcessRunner` over `utils.git_runner` (SSH-safe env, `safe.directory`, credentials, cancel scope, retry, git invoker) |
| ripgrep | `BoundedProcessRunner` (`seams.ripgrep`) | `SubprocessExecutorRunner` over `SubprocessExecutor` (byte cap, deadline) |
| clone | `CloneBackendSettings` with `CloneServices` (`seams.clone`) | `core_clone_services()`: cancellable process runner, credential redaction, NFS visibility barrier |
| content, content writer, index store | none | n/a |

## Resolution

One entry-point group per seam. The ENTRY NAME is the mode it implements:

| Mode | Entry name |
|---|---|
| `local` | `local` |
| `delegated` | `delegated`; also any name that is not another mode of the seam (`workspacemcp`, `gitmcp`, `sandbox`, `orchestrator`), for delegate wheels published before this rule |
| seam-specific (`ontap`, `cow-daemon`, `delegated-plugin`) | that name |

Order: the seam's env var (e.g. `CIDX_CONTENT_BACKEND`; delegate modes only), then
exactly one entry point satisfying the mode. None, or more than one, raises
`SeamUnavailableError` naming the seam and the group. A provider that is
configured but cannot be loaded raises the seam's `SeamPluginError` subclass.
There is no built-in fallback: the core holds no implementation it could fall
back to.

Existing delegate plugins keep resolving without a release: their entry names
are accepted as `delegated`. New delegates register as `delegated`.

## Seam status

| Seam | Protocol (SDK) | Group | Entry names and where they live | Conformance (`cidx_testkit.conformance`) |
|---|---|---|---|---|
| content | `ContentBackend` | `code_indexer.content_backend` | `local`: `cidx-backend-workspace` | `content`: local, fake workspace-mcp, both as resolved |
| content writer | `ContentWriter` | `code_indexer.content_writer` | `local`: `cidx-backend-workspace` (`LocalContentWriter`), reached through `content_writer_for(source)`, see below | `content_writer`: local, fake workspace-mcp, both as resolved |
| git | `GitBackend`, `GitCommandRunner` | `code_indexer.git_backend` | `local`: `cidx-backend-git`; delegate `gitmcp` (`plugins/cidx_git_backend`) | `git`: local, fake git-mcp, both as resolved |
| git invoker | `GitInvoker` | `code_indexer.git_invoker` | none local (no invoker = native git) | `git_invoker`: fake git-mcp as resolved |
| ripgrep | `RipgrepBackend` | `code_indexer.ripgrep_backend` | `local`: `cidx-backend-workspace` | `ripgrep`: fake workspace-mcp; local and delegated as resolved; Pact unchanged |
| search | `SearchBackend` | `code_indexer.search_backend` | none local (local = the query engine, no backend object) | none: no delegate or fake exists |
| index store | `IndexStore` | `code_indexer.index_artifacts` | `local`: `cidx-backend-workspace` (an `IndexStore`); a delegate here is the artifact client the core wraps in `DelegatedIndexStore` | `index_store`: local; `DelegatedIndexStore` over the fake as resolved |
| SCIP | `ScipBackend` | `code_indexer.scip_backend` | none local (the per-language indexers in `cidx_scip`'s `SCIPGenerator`, built by `cidx_scip.server.backend_wiring`); resolved by `cidx_scip.server.backend_plugin` | `scip`: fake sandbox-mcp (two replicas, shared result store), raw and as resolved |
| X-Ray | `XrayBackend`, `XrayInlineExecutionBackend` | `code_indexer.xray_backend` | none local (the engine is `cidx_xray`'s, built per request); resolved by `cidx_xray.server.backend_plugin` | `xray`: fake sandbox-mcp, raw, as `cidx_xray.server.backend_wiring` builds it, and under `PodLocalInlineXrayBackend`; local engine not run (needs xray-cli and rustc) |
| clone | `CloneBackend` | `code_indexer.clone_backend` | `local`, `ontap`, `cow-daemon`: `cidx-backend-workspace`; `delegated`: `DelegatedCloneBackend`, registered by the core distribution | `clone`: local; the core's delegated client over the fake workspace-mcp; the fake's client as resolved. ontap and cow-daemon: resolution tests only |
| CLI invoker | `CliInvoker` | `code_indexer.cli_invoker` | none local (no plugin = built-in CLI); delegate `orchestrator` (`plugins/cidx_orchestrator_invoker`) | `cli_invoker`: the orchestrator delegate against the fake orchestrator (loopback HTTP), in its plugin suite and as resolved |

All seams resolve through `SeamResolver`. The clone seam reads no env
override: `CIDX_CLONE_BACKEND` carries a mode name for the cluster scripts, not
a `module:factory` path. An unknown `clone_backend` value now refuses at
startup instead of becoming `local`.

ONTAP and the CoW daemon stay in `cidx-backend-workspace`; the end state lists
`cidx-clone-ontap` and `cidx-clone-cow`. They share the deletion helpers and
the injected services, no deployment needs one without the other, and two more
distributions would add CI jobs, Dockerfile copies and path dependencies for no
installation choice anyone makes today. Splitting them is mechanical when one is
needed.

The delegated clone backend composes the local one (snapshot registry,
deletes), so a delegated pod still installs `cidx-backend-workspace` until
`cidx-delegate-workspace` exists; its `delegated` entry then moves there (two
`delegated` entries refuse at startup, so a leftover cannot run silently).

A delegate reads what it needs from the core through the SDK, never by import:
the CLI invoker delegate asks `cidx_sdk.seams.cli_invoker.REPOSITORY_SOURCE`
(installed by `server/services/cli_invoker_plugin.py`) whether this pod holds
the repositories and which golden repository a `cwd` is.

## Source-tree runs

The resolver reads installed distribution metadata. A test run from the source
tree registers what the in-repo distributions DECLARE in their
`pyproject.toml` (`cidx_testkit.registry.register_in_tree_entry_points`, called
from `tests/conftest.py`), so it resolves seams exactly as `pip install .`
would. A child process gets the same through
`cidx_testkit.registry.child_pythonpath`, which writes `*.dist-info` metadata
for them. An editable install made before a distribution was added must be
reinstalled (`pip install -e .`) for the CLI to resolve the new providers.

## Conformance and guards

* `cidx_testkit.conformance.content.ContentBackendContract`: subclass it and
  provide `content_subject`. Run against `LocalContentBackend`
  (`packages/backends/cidx-backend-workspace/tests`), the fake workspace-mcp
  (`packages/cidx-testkit/tests`) and both modes as the core resolves them
  (`tests/unit/seams/test_content_seam_resolution.py`).
* Every other suite has the same layout: the local implementation in its
  backend's `tests/`, the fake in `packages/cidx-testkit/tests`, and both
  modes as the core resolves them in `tests/unit/seams/` (git, clone, SCIP,
  X-Ray, CLI invoker, and `test_workspace_seams_resolution.py` for ripgrep,
  content writer and index store). Delegates that live in `plugins/` run the
  suite in their own directory too.
* `cidx_testkit.fakes.workspace_mcp`: an in-process workspace-mcp (manifest,
  fetch, write, rg, clone, index artifacts) with its remote-only answers (400
  for an absent root, EACCES under a missing directory, ECONFLICT writes);
  `register_as_delegate` makes it the `delegated` provider.
* `cidx_testkit.fakes.git_mcp`: a git-mcp stand-in for the git invoker and
  the git backend.
* `cidx_testkit.fakes.sandbox_mcp`: sandbox-mcp's `sandbox_run_profile` and
  `sandbox_result_fetch` (result handle paging), for SCIP and X-Ray. Replicas
  share one `SandboxResultStore`, and the clients round-robin across them, so
  a page is fetched from a different replica than the run (cluster-aware
  state).
* `cidx_testkit.fakes.orchestrator`: the agent orchestrator as a real HTTP
  server on the loopback interface (login, submit with 429 and 401, job
  polling), for the CLI invoker.
* Static guard engines (`cidx_testkit.guards`): git seam, content-seam
  inventory, index-store ratchet. They walk a `SourceTree` spanning the in-repo
  distributions, so code moved out of `code_indexer` is still scanned (keyed
  `<package>/<path>`). The core's policy and test entry points stay where they
  were.
* The workspace-mcp ripgrep Pact stays in
  `tests/unit/global_repos/test_workspace_mcp_content_rg_contract.py` until the
  delegate distribution exists.

## Authorization projection above the seam

Implementations never filter by user. A catalog-style access projection is
applied by the caller layer, once, above the Protocol, so local and delegated
answers are projected identically. The catalog projection lives in
`server/services/cidx_meta_access/` and attaches at `FileListingService` (file
listing and reads) plus the cached-handle reprojection in `cached_payload`.
Features reach it only through the `access_projection` capability (the wiki
does). `tests/unit/ci/test_catalog_guard_presence.py` pins every call site.

## Callers hold a source

`content_seam_source(repo_path)` (`server/services/file_service.py`, and
`kernel.capabilities.content_seam_source` for features) always returns a
`ContentSeamSource`: the delegated backend's (a versioned snapshot maps to its
base clone), or the content seam's `local` implementation at the path itself
(the server's local backend; outside a server, and for a pod-local `local://`
repository under a delegated backend, the local mode's module resolved once
per process). No caller asks a server-wide "delegated" flag to decide how to
read; it asks the source it holds.

Whether the files are a directory on this pod is the implementation's answer,
`pod_directory(source)` (from `ContentBackend.is_local`). The local
implementation's readers keep behaviour the manifest and fetch Protocol does
not have, so they are kept for such a source rather than routed through
`LocalContentBackend`:

| Reader / writer | Local behaviour kept | Where the choice is made |
|---|---|---|
| `FileListingService` list, level, read | walk follows links inside the repo; index fast path; confinement resolves links; text hash of local reads | `_files_of`: `_PodDiskFiles` or `_SeamFiles`, once per call |
| `FileCRUDService` create, edit, delete | link-resolving confinement, `.git`/index-dir refusal, BOM-preserving atomic write | `_writes_for`: `_PodDiskWrites` or `_WriterWrites` |
| directory tree, X-Ray, SCIP context, wiki, depmap sizes, composite and activated-copy listings, repository statistics, semantic-search chunk reads | disk walks that follow links | the wrapper returns `None` when `pod_directory` is set |
| activated copy present | the copy's directory | `activated_copy_present` (registry for a copy elsewhere) |

Gitignore, exclusions, `indexed_only` (cidx's own index state, always on this
pod) and the catalog projection stay above the seam in `FileListingService`,
identical for both readers.

Writes go to the writer that writes where the source reads:
`content_writer_wiring.content_writer_for(source)` is `LocalContentWriter`
(`resolve_content_writer_for("local")`) for the local implementation and the
writer parked at startup for a delegated one; with none parked it raises,
never writing this pod's disk. `working_tree_writer(path)` is the same for a
working tree (None when it is this pod's disk). Golden-repo CRUD, untracked
file removal in `git_pull_updater` and `exclude_index_state` use them.
`server_content_writer()` keeps meaning "the parked delegated writer": the
metadata outbox's `workspace-mcp` sink still refuses without one.

### Branches that stay, and why

`is_content_seam_delegated()` remains where the question is not "how do I
read or write this content" but where something else runs or is authoritative.
Each keys on the content implementation's own declaration
(`server_content_backend().is_local`), not a separate flag:

| Group | Call sites | Why it stays |
|---|---|---|
| git placement | `git_operations`, `git_read`, `_legacy` git path resolution and `_has_git_dir`, `git_tools_seam.delegated_git_tools`, `index_freshness`, `branch_service`, `progress_subprocess_runner`, `temporal_search_service`, `git_subprocess_env`, `activated_repos` branches, `inline_repos` branch errors | git-mcp versus local git is the git seam's caller step, not content |
| refusals (product decisions, delegated only) | `delegated_refusal`/`refuse_when_delegated` (repo analyzer, lifecycle analysis, git tools on golden repos, branch listing), `cli.py` global activate, depmap `analysis_refusal`, cidx-meta backup, X-Ray in-pod execution and line-content refusals | the feature needs the repository on this pod; the refusal is the capability's answer for a delegated implementation |
| authority of this pod's disk for lifecycle | `refresh_scheduler`, `git_pull_updater._working_tree_is_elsewhere`, `golden_repo_manager`, `golden_repo_reconciler`, `versioned_snapshot_reclaim`, `workspace_registration_sync`, `activated_write_reindex`, `metadata_reader`, `repository_listing_manager` activation count, `activated_working_copy.working_copies_for` | destructive or orphan decisions: a directory missing here proves nothing for a delegated implementation |
| path resolution without a filesystem check | `_utils`, `_legacy._resolve_repo_path`/`_registry_index_path_dir`, `repo_search` | alias targets on a delegated pod hold index state only |
| no local directory to watch, create or configure | `files.py` auto-watch, memory `mkdir`, depmap lifecycle dir and coordination dir, `inline_repos` copy config, `cidx_meta_access.path_policy` read mirror, `file_service._delegated_activated_path` | pod state for a repository that is elsewhere |
| capability hosts | `kernel_wiring.ServerContentSeamHost.is_delegated`, `cli_invoker_plugin.holds_repositories` | they answer the question for features and delegates |
