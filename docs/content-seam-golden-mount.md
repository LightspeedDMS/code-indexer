# Content seam: cidx without the golden-repos volume

With every repository backend delegated -- `content_backend`, `git_backend`,
`clone_backend`, `ripgrep_backend`, and every enabled feature's `*_backend`
bootstrap setting (X-Ray's `feature_bootstrap_settings.xray.xray_backend`) -- plus
`index_store_backend: delegated` (and `scip_backend: delegated-plugin`),
`<data>/golden-repos` on a cidx pod holds **this pod's index state only**: what
it built, or checked out of the IndexStore
([index-store-cross-pod.md](index-store-cross-pod.md)). Repository content lives
with workspace-mcp / git-mcp at the same absolute paths. Nothing cidx needs is on
the shared golden-repos volume, so the volume can come off the cidx StatefulSet.
With any backend `local` (the default) nothing here applies.

## Startup check (`server/startup/golden_mount_guard.py`)

| Condition (all-delegated only) | Result |
|---|---|
| A path cidx writes its own state to is under golden-repos | startup **refused** (`GoldenMountDependencyError`) |
| golden-repos is still a network mount (nfs, nfs4, cifs, ...) | WARNING: nothing needs it, it can be removed |
| `scip_backend` other than `delegated-plugin` | WARNING: SCIP builds would read this pod's empty directory |
| Mount table unreadable | WARNING (never a guess) |

Checked state paths: the PayloadCache database and the snapshot-reader lease
directory. Both live in `<server_data_dir>/data/.pod-state/` when all-delegated.

## What changed, per consumer

| Consumer | Under all-delegated |
|---|---|
| `GoldenRepoManager.get_golden_repo_indexes`, `_index_exists` (MCP `get_golden_repo_indexes`, REST `/api/admin/golden-repos/{alias}/indexes`) | read the alias target with its published version served here (`served_golden_target`); no pointer is not-found, an unpublished pointer or store failure raises |
| MCP `get_index_status` on a `-global` alias | the same served target (`_served_global_index_path`) instead of the base clone directory |
| `cidx.repos.indexed` gauge (`counts_as_indexed`) | the alias records a published version (registry); nothing is checked out |
| Refresh "Reconciled registry with filesystem at START" | the recorded version is served first; with nothing here and nothing recorded the reconcile is skipped (never an auto-disable of temporal/SCIP from an absent directory). END reads the snapshot this pod just built |
| Golden add, post-clone workflow | no wait on the mount (`_await_clone_path_locally_visible` runs only when the clone is on this pod); `cidx init`/`cidx index` run in the pod's index-state directory, reading through the seams, and the add publishes (`_publish_added_index`) |
| Activation index seeding (`_seed_index_state`, `seed_index_from_golden`) | the golden alias's published version is checked out straight into the copy's index root; golden-repos is not read. A pointer without a recorded version falls back to the index this pod holds, or fails |
| cidx-meta refresh / `exit_write_mode` | the latest published version is checked out to the pod's `cidx-meta` first; with nothing published, `cidx init` there builds and publishes the first version (a failed init fails the refresh). The snapshot comes from the IndexStore, never `LocalCloneBackend` |
| PayloadCache | `.pod-state/payload_cache.db` when backend-less; with a storage backend registry the entries are in the registry database, shared by every pod (cross-pod handle reads work) |
| Snapshot-reader leases | pod-local (`use_pod_local_lease_directory`): every snapshot a reader holds is a checkout on this pod; the legacy in-tree directory is neither read nor swept |
| Startup golden-volume NFS monitoring | skipped |
| Startup `.versioned` orphan sweep (Bug #1567) | skipped under a delegated IndexStore: the checkout sweep and served-index cache prune each pod |
| FTS and semantic backfill sweeps (and their queued build jobs) | disabled under a delegated IndexStore: an in-place build would change one pod's checkout without publishing it; add and refresh build and publish |
| GAP-4 index staleness pass | disabled under a delegated IndexStore: it reads index metadata this pod may not hold; each refresh asks git-mcp whether upstream moved |
| REST git cat/blame/history, user wiki, activated index status and health | through the git seam / content seam / registry; see [content-seam-activated-repos.md](content-seam-activated-repos.md) |
| SCIP result `context` lines (every `scip_*` MCP tool, REST, composites: `DatabaseBackend._read_context_lines`) | fetched through the content seam (`file_service.content_seam_source` at the repository path above `.code-indexer/scip`, a snapshot mapped to its base clone; `backend.fetch` in batches of 64 files). Stored paths are confined lexically (`lexical_relative_path`; outside the root or into `.git` refused, never fetched). Soft: a missing file has no context; a file the backend cannot serve, and a backend that is unavailable, each log one WARNING and leave `context` None |
| SCIP build: `drop_git_backed_documents` | with a non-local `BuildFileSource` (content seam delegated) decided from the stored paths alone; a symlink into `.git` cannot be judged on the pod, and the content backend never follows one |

## Composite queries, temporal status and git-mcp

* Composite `cidx query`: the server stamps `--git-backend`/`--content-backend`
  (`server_backend_args`) on each member's `cidx query`. The branch filter
  (`GenericQueryService`) then asks git-mcp `git_exec` (`rev-parse
  --is-inside-work-tree`, `rev-parse --abbrev-ref HEAD`, `rev-parse HEAD`,
  `ls-tree -r --name-only HEAD`, `merge-base --is-ancestor`). Staleness is
  skipped with `--content-backend=delegated` (no per-file lookup exists, and a
  whole-repository manifest per query is too costly); results carry no
  staleness rather than "deleted". git-mcp unreachable
  fails the query (`BranchContextUnavailableError`), never an unfiltered result.
* Temporal status (activated index status, REST health indexes, the activated
  index manager): `get_temporal_repo_status` serves the recorded temporal
  version first. When temporal is enabled but the alias records no temporal
  version and this pod holds no root (`require_temporal_answerable`), the answer
  is "not served here" -- 503, or `status: not_served_here` -- never "absent".
* git-mcp policy (git-mcp `git-exec.policy.ts`, `git-api.service.ts`):
  `blame --porcelain` and `log --follow --format=... -n<N>` are on the
  `git_exec` allowlist; `git_rev_parse` takes free args; `git_show_objects`
  takes hexadecimal object ids only, so REST `git cat` shows the commit
  `rev-parse` resolved, never the caller's symbolic revision.

## SCIP rows

No `scip/` row reads the repository (`repo-read`). Context lines and
`drop_git_backed_documents` are migrated (above); `LocalBuildFileSource`
(discovery) is unreachable on a mount-less pod: every server caller passes
`ContentBuildFileSource` (`scip_repo_reads`) when the content seam is delegated.

## Refresh, backfill and cleanup edge cases

| Situation | Behaviour |
|---|---|
| Empty golden repo (unborn HEAD, no branch upstream) | every refresh (forced or not) returns success with `skipped: empty_repository` (`GitPullUpdater.is_empty_repository`, local git or git-mcp); the IndexStore backfill reports it under `golden_skipped` and queues nothing. Its first upstream commit ends the skip |
| Registered branch deleted upstream | refresh fetch is `category=permanent` and names the branch and `change_golden_repo_branch`. `change_branch` fetches only the target (`fetch(ref=...)`, git-mcp `git_fetch` `ref`) and, when the clone's plain fetch names a vanished branch, points `remote.origin.fetch` at the target |
| `failed_files` above `failed_file_paths` (count-only) | owes a retry (`RecordedFailures.unattributed`, same owed-retry backoff); that refresh runs `--reconcile`. A refresh with NO upstream change runs it too: owed failures (an unattributed count, or retryable paths) are a stale-index signal (`OWED_FAILURES_SIGNAL`), bounded by `admit_forced_reconcile`, so a record from before the fix heals. Full index runs now record their failed paths |
| Two pods cleaning the same snapshot | a path whose durable pending-deletion row a peer removed is dropped (job completed, not failed); a tree already gone -- before or mid-rmtree, or a workspace-mcp 404 naming the path -- is deleted |
| Control-plane metadata import before the content backend is wired | INFO "will retry"; WARNING only on the last attempt or another failure |
| SCIP index built by the delegated plugin (records `file:///workspace`) | the query's project root comes from where the `.scip.db` sits (`<repo>/.code-indexer/scip/<sub-project>`), never the index metadata; plugin builds also record the project's own directory. Context lines resolve and `project` names the repository |
| Delegated X-Ray on a `.versioned` alias | Phase 2 (scan and graph) is asked at `golden_repo_content_root` like Phase 1 (`_ContentRootXrayBackend`); in-pod xray-cli `files_errored` is a whole-batch error when nothing parsed; otherwise the results stay one per file spec -- a candidate that is not a readable file gets its own `XRayFilesErrored` error, failures nobody can name are one batch-level count inside the first result. Pod-local (inline) scans answer an unreadable candidate here and never send it to the sandbox, so batching never changes the answer |
| Checkout sweep of a removed copy / golden repo | the emptied target, its `.checkout.lock` / `.readers.lock` and an emptied `<user>/` or `.versioned/<repo>/` go too, under both locks with no reader; residue an earlier pass left is cleared the same way. Lock waiters re-open a lock file deleted under them (`flock_live`) |
| `.code-indexer/` in activated copies | activation excludes it (local: `.git/info/exclude`; delegated: `.code-indexer/.gitignore` of `*` via the content writer -- workspace-mcp never writes under `.git`); `git_status` never lists it and a stage of the whole tree excludes it first |

## Tests

`tests/unit/server/test_golden_mount_removal.py` (each consumer local vs
delegated with the pod's repository directory absent and
`forbid_golden_content_io` raising on any content access, fakes in
`tests/unit/server/golden_mount_fakes.py`),
`tests/unit/server/startup/test_golden_mount_guard.py`,
`tests/unit/server/repositories/test_activated_index_store_cross_pod.py`,
`tests/unit/server/test_activated_git_wiki_status_seam.py`,
`tests/unit/server/mcp/test_global_alias_handler_fallbacks.py`,
`tests/unit/server/services/test_temporal_not_served_here.py`,
`tests/unit/services/test_composite_query_backends.py`,
`tests/unit/scip/test_scip_context_content_seam.py`,
`tests/unit/global_repos/test_refresh_empty_repository.py`,
`tests/unit/server/repositories/test_change_branch_off_deleted_branch.py`,
`tests/unit/services/test_failure_count_recorded_with_paths.py`,
`tests/unit/global_repos/test_cleanup_already_deleted_evo64616.py`.
