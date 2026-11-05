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

## Restricted callers (catalog projection, v12.93.0)

Upstream's `cidx_meta_access` judges every catalog door per caller. Under
`delegated` the same judgement runs BEFORE any seam read, on every door:

| Door | Under delegated |
|---|---|
| `FileListingService` reads/listings (`_read_confined`, `list_files_by_path`, `list_directory_level`) | `view_for` / `lines_view` / `listable` exactly as locally; `size` and `content_hash` of a projected file are of the projected text |
| REST `?content=true` (v2), `/api/repos/{alias}/files` (v1), composite listing | judged before the seam fetch/listing; a composite member's listing reports the walked directory as its `base` |
| Wiki routes (root, article, asset, sidebar, index) | the `CatalogWikiView` is built before the seam branch; restricted pages are projected per request, never cached |
| `GET /api/repositories/{alias}/description` | judged, then read through `SharedCidxMeta` (single-file only) and served via `served_text` |

The projection's dependency-map model and membership read
`path_policy.catalog_dep_map_dir`: the read mirror above under `delegated`
(the served snapshot holds index state only), `<root>/dependency-map`
locally. A mirror sync failure raises; it never projects against an empty map.
Tests: `tests/unit/server/{mcp,routers,wiki}/*meta*_delegated*.py`,
`test_cidx_meta_{get_content,list_files,browse}_access.py`.

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

## Control-plane metadata files (aliases, write-mode markers)

Alias pointers and write-mode markers live in the registry. Their legacy file
copies (`golden-repos/aliases/<name>.json`, `golden-repos/.write_mode/<name>.json`)
are still read by the startup import, by the read-through on a registry miss
before that import finishes, and by `metadata_read_source=files` (which also
writes them). Under `content_backend: delegated` the installed
`ControlPlaneMetadata` uses `SeamMetadataFiles`
(`server/services/control_plane_metadata_seam.py`, EVO-64616): the same paths
through `SharedCidxMeta` -- `/content/manifest` + one batched `/content/fetch`
for reads, the content writer for writes -- never this pod's disk. A backend
that is not built yet or cannot be reached raises
`MetadataFilesUnavailableError` (an `OSError`): the import counts it as a
failure and retries, so the read-through stays on rather than "no aliases".
The read-through is best effort on top of the registry: when the store is
unreachable (for example after `start_import` gave up), `get` and `list_all`
log the outage (WARNING once per kind, then DEBUG) and return the registry's
answer instead of failing. Under `metadata_read_source=files` the files are
authoritative and an outage still raises. `list_all` skips a file that is gone
(ENOENT), warns and skips an unreadable one (EISDIR, EFBIG), and raises on any
other per-file fetch error.
A directory not created yet (`.write_mode/` before the first write session)
lists as empty, like a missing local directory: workspace-mcp answers that
manifest with `400 source_path does not exist`, matched narrowly by
`content_backend.is_absent_root_error`. Every other manifest failure raises.

`metadata_for` matches the installed directory lexically OR after resolving
symlinks (several callers derive golden-repos from the realpath'd
`activated_repos_dir`), and logs once per directory when a caller asks for a
different directory while a registry-backed component is installed -- that
caller is reading its aliases from local disk.

workspace-mcp must allow writes under `aliases/` (and `.write_mode/` if marker
files are written) in `WSM_CONTENT_WRITE_ROOTS` for the rollback mode and the
outbox replay.
