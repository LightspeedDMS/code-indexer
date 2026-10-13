# Content seam: cidx-meta on a mount-less pod

cidx-meta (`<golden_repos_dir>/cidx-meta`, aliases `cidx-meta` /
`cidx-meta-global`) is the non-git repository cidx writes itself:
repository descriptions, memories, the dependency map, X-Ray patterns. With
`content_backend: delegated` the pod does not mount the golden-repos volume,
so its own `golden-repos/cidx-meta` holds **index state only**, and the
content lives with workspace-mcp at the **same absolute path** -- where the
write tools (`enter_write_mode` on `cidx-meta-global`) already put it.

With `content_backend: local` (the default) nothing here applies: every
writer, reader and the refresh run exactly as before.

## The seam

`server/services/cidx_meta_content.py`:

| Call | Local backend | Delegated backend |
|---|---|---|
| `shared_cidx_meta(meta_dir)` / `shared_cidx_meta_for(path)` | `None` -- caller keeps its disk code | `SharedCidxMeta`; raises if no content writer is parked |
| `read_bytes/read_text/exists/blob_sha` | -- | `/content/fetch`; absent is `FileNotFoundError`, anything else `CidxMetaUnavailableError` |
| `entries/list_names` | -- | `/content/manifest`, read now (never cached); failure raises |
| `put/delete/rename` | -- | content writer (`POST /content/write`); `expected=` is the git-blob CAS (`None` = must not exist); `ECONFLICT` is `CidxMetaConflictError` |

Rule: an unreachable workspace-mcp is never "cidx-meta is empty".

## Writers and readers

| Consumer | Under delegated |
|---|---|
| `meta_description_hook.atomic_write_description`, `on_repo_added`, `on_repo_removed` | writer put/delete; fact-check scratch file in the pod's temp dir |
| `lifecycle_batch_runner` (`write_meta_md`, `_process_one_repo`, `LifecycleFleetScanner`) | reads/writes through the seam |
| `MetaDirectoryUpdater` | lists the manifest; stub create is "must not exist" (never overwrites a description another pod wrote) |
| `DescriptionRefreshScheduler` (suffix migration, terse scan, has-description) | seam; migration rename never overwrites |
| `memory_io` (create/edit/delete/read) + `MemoryStoreService`, metadata cache, retrieval hydration, access filtering | seam; edit/delete CAS on the bytes whose content hash the caller checked -- a write by another pod in between is `StaleContentError` |
| `ClaudeCliManager` README-fallback catch-up | seam rename; no git commit (the pod's dir is not the content's repository) |
| `routers/diagnostics` generate-missing-descriptions, `routers/repository_health` description | seam (route read off the event loop; backend failure is a 500, not a 404) |
| refresh `_queue_missing_description` | seam `exists` |

Locks are unchanged: `write_lock("cidx-meta")`, the per-alias lifecycle lock,
the per-memory lock and the refresh debouncer wrap the same calls. The CAS adds
cross-pod safety where a lock is per-pod.

## Indexing and change detection

* `index_command_layout.repo_layout_kwargs(url, alias)`: cidx-meta spawns get
  `--content-backend=<configured>` and **git stays local** (git-mcp does not
  hold cidx-meta). Under `local` this stamps exactly what `local_content=True`
  did. Other `local://` repos stay entirely pod-local.
* `RefreshScheduler._has_local_changes`: cidx-meta under delegated lists the
  shared manifest (pruned of `.code-indexer`/`.git`, visible files only) and
  compares its fingerprint (paths + blob shas) with the one published WITH the
  current alias pointer (`content_fingerprint` in the alias document, i.e. the
  control-plane registry -- shared by every pod, a restarted one included).
  No clock is consulted; deletions count; no published fingerprint (first
  index) is a change. A refresh publishes the fingerprint it detected in the
  same write as the new pointer, so a failed refresh publishes nothing. A
  listing failure fails the refresh. Extension-drift scanning uses the
  manifest too.
* Empty cidx-meta: "No files found to index" is tolerated only when a fresh
  manifest listing succeeds AND shows no visible file.
* Snapshot/publish: unchanged -- the index is built in the pod's directory and
  published through the IndexStore exactly as for any delegated repo.

## Git backup: refused under delegated

`cidx_meta_backup` commits and force-pushes the cidx-meta directory. On a
mount-less pod that directory is index state only, so a backup would push an
empty tree over the real one, and git-mcp does not hold cidx-meta. So
`CidxMetaBackupSync.sync()` and `CidxMetaBackupBootstrap.bootstrap()` raise
`CidxMetaBackupUnsupportedError`; the refresh still indexes the shared content
and then fails with that reason on every cycle (changed or not) until the
backup is disabled. Backing up shared content needs a workspace-mcp side
mechanism (it owns the files); out of scope here.

## Dependency map and X-Ray patterns

| Part | Under delegated |
|---|---|
| Readers: depmap MCP tools + `DepMapMCPParser`/graph, domain and dashboard services, health detector, quick-reference section, dashboard/health routes | Read `cidx_meta_read_path`, which is the **read mirror** `<golden_repos_dir>/.cidx-meta-read-mirror`: `dependency-map/**` and the top-level descriptions, synced from the manifest (only changed blob shas fetched, removed files dropped; at most every 5 s). This pod's own writes through the seam are applied to the mirror at once (read-your-writes; a failed local apply forces a re-sync on the next read); other pods' writes appear within 5 s. A sync failure raises; never an empty map. Local: the versioned snapshot, as before. |
| Writers: full/delta analysis, refinement, repair (incl. graph-repair dry run), lifecycle pre-flight, scheduler | **Refused** (`dep_map_delegated`): the Claude agent runs with `cwd` in golden-repos and reads the repositories with its own file tools, so on a mount-less pod it would publish an empty or partial map. Routes answer 501 and the MCP trigger an error BEFORE any sentinel is claimed; the service methods raise; the scheduler is not started. |
| Analysis sentinel (`SharedJobSentinel`) | Same protocol on the writer's CAS: claim = create-if-absent, stale replace and release conditional on the bytes read. |
| Dashboard cache (`_dashboard_cache.json`) | `SharedContentDashboardCacheBackend`: same file and interface; job-slot claim/swap are real CAS. |
| Startup (staging cleanup, eager dir, stale-sentinel sweep) | Skipped: the pod's directory is not the map. |
| X-Ray patterns (store/load/delete/seed) | Through the seam; store without overwrite and seeding are "must not exist". **No git commit**: it only fed the cidx-meta git backup, refused under delegated, and git-mcp does not hold cidx-meta. |

Why refuse rather than redesign the agent: giving it repository context
through cidx's MCP tools (browse/get_file_content/search) instead of a local
tree changes its prompts, tool budget and output quality -- a product change
to plan and evaluate, not a seam change. Until then analysis runs only where
the repositories are mounted; the map it produced stays readable everywhere.

## Still on the pod's directory

| Area | Why |
|---|---|
| `bootstrap` override YAML, `claude_cli_startup` mkdir, `nfs_self_check` | index config and probes of the pod's own directory, not content |
| `memory_retrieval_pipeline` `.resolve` | path confinement on the pod path; the read itself is through the seam |
| `frontmatter_verifier` | no server caller |

## Deployment

* workspace-mcp `WSM_CONTENT_WRITE_ROOTS` must cover `<golden_repos_dir>/cidx-meta`
  (the golden-repos root already covers it), and its read roots must serve the
  same path.
* Disable `cidx_meta_backup` on servers with `content_backend: delegated`.
* Inventory: `tests/unit/server/content_seam_inventory.json` rows of every
  migrated function are class `migrated`, noting this seam.
