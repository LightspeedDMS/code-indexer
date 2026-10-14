# Content seam: activated repositories on a mount-less pod

An activated repository is a user's writable copy of a golden repository at
`<activated_repos_dir>/<username>/<user_alias>`. With `content_backend: local`
(the default) nothing here applies: activation, inspection and deactivation run
exactly as before (`LocalWorkingCopies`).

With `content_backend: delegated` the pod has no `activated-repos` volume. The
working copy exists only on workspace-mcp's storage, at the **same absolute
path**, and every consumer reaches it through a seam addressed by that path.

## Addressing and isolation

| Concern | How |
|---|---|
| Path | Built server-side from `(username, user_alias)` by `_safe_user_scoped_path`, exactly as today. A user never supplies a path. |
| Who may touch it | Unchanged: every front door checks the registry (`_load_metadata` / `user_has_activated_repo`) for THIS user before using the path. |
| Containment on the storage side | workspace-mcp re-checks every path against `WSM_CLONE_ALLOWED_ROOTS` (literal and realpath'd parent chain), refuses an allowed root or a directory containing one, never follows a symlink at the path. |

## Lifecycle (`server/repositories/activated_working_copy.py`)

| Step | Local | Delegated |
|---|---|---|
| Activate: copy | CoW clone on this disk | `POST /clone-path` with `if_absent: true` (409 = existing copy, never replaced) |
| Activate: git | git in-process | git-mcp by `cow_path` (refresh index, remotes, fetch golden, checkout, user.email) |
| Activate: index | `.code-indexer` comes with the clone | golden repo's index on THIS pod copied to the copy's index root (pod state), then `cidx fix-config`; no golden index on the pod is an error |
| Exists? | `os.path.exists` | `POST /clone-path/stat` |
| Branch list / switch / sync status / conflicts | raw git + git seam | git seam only (`list_branches`, `log_raw`, `rev_parse`, `status_porcelain`) |
| Deactivate | drain, rename to `.trash`, purge | drain, `POST /clone-path/delete`, drop pod index state, THEN the registration (a failed delete keeps it: no ghost) |
| Failed activation | rmtree (+ late-materialization grace loop) | one delete (workspace-mcp's clone is synchronous) |
| Finders (`find_repos_by_golden_alias`, `find_by_canonical_url`) | per-user dirs | the registry |

## Composite repositories

A composite is `<user>/<alias>/` with one working copy per member,
`<alias>/<member>`. Delegated, it lives on workspace-mcp like any copy.

| Step | Local | Delegated |
|---|---|---|
| Exists? | `Path.exists` | `POST /clone-path/stat` |
| Proxy config | `ProxyInitializer.initialize` + discovery walk | `create_proxy_config` under the pod's `index_root(composite)`; members = sorted activated aliases (`set_repositories`), no walk |
| Members | CoW clone each | the single-activation delegated clone per member (workspace-mcp creates the composite dir with the first one) |
| Rollback | rmtree | one `/clone-path/delete` of the composite + pod index state of every member |
| Deactivate | rename to `.trash`, purge | `_do_deactivate_delegated`; pod index state of every member the proxy config names |
| File listing (`_list_composite_files`) | walk each member | manifest per member (never cached); members from the registry |
| Details (`_get_composite_details`) | walk each member | manifest sizes + pod index state; members from the registry |

Fail loud: a member missing on workspace-mcp, an unreachable backend or a
manifest entry without an mtime raises; a listing `path` that is absolute or
contains `..` is refused. Known differences, both because the manifest lists
files only and never follows a symlink: a directory in a single-level listing
reports the newest mtime beneath it (locally its own), a symlink its own size
and mtime (locally its target's); details' size excludes `.git` (the manifest
never walks it).

Refused under delegated (fail loud): a bare golden clone;
a `clone_backend` other than `delegated`; `git_backend: local`. An unreachable
workspace-mcp raises -- it is never "repository not found".

## Reindex after edits (no change feed)

| Edit | Local | Delegated |
|---|---|---|
| cidx file tools: MCP `create_file`/`edit_file`/`delete_file`, REST `/api/v1/repos/{alias}/files` | auto-watch, started before the write, re-indexes | after a successful write, an incremental reindex (`semantic`, `fts`; `clear=False`) of the copy is enqueued through `trigger_reindex` (`server/services/activated_write_reindex.py`) |
| cidx git tools that move the working tree: merge, merge abort, branch switch, checkout file, reset, clean, pull, stash push/pop/apply (MCP and REST) | an already-running auto-watch re-indexes; otherwise nothing | the same enqueue (paths `["*"]`), also after a merge or pull that stops on conflicts (git has rewritten the tree) and after a failed stash pop/apply (it may have) |
| Anything outside cidx: a process writing workspace-mcp's storage, a commit landing on the remote (the copy changes only when cidx pulls) | the watcher sees local writes only while auto-watch runs | **not seen** -- no change feed. Indexed only by `trigger_reindex`, `sync_repository`, or the incremental pass of the next reindex a cidx write enqueues |

Per copy the enqueue coalesces: a PENDING reindex of the copy has not read it
yet, so the write joins it (`coalesced`); a RUNNING reindex (of the copy, or any
of the user's copies -- `trigger_reindex` allows one per user) may have read past
the written paths, so one follow-up is scheduled for when it is done
(`deferred`); otherwise a new job (`queued`). The MCP response carries the outcome
as `reindex` (REST response models drop it). Fail loud: `failed` (with `error`)
is also logged at ERROR as SVC-REINDEX-001, a follow-up that never gets a slot
as SVC-REINDEX-002. Every enqueue is gated on the registry
(`user_has_activated_repo`) for the calling user; golden repositories in write
mode (`-global`, write exceptions) are re-indexed by their refresh on
`exit_write_mode`, not here. Under `content_backend: local` none of this runs and
responses are unchanged.

Follow-ups waiting for a slot are in-memory timers: a pod restart drops them.
The written paths are then indexed by the next reindex of the copy --
`trigger_reindex`, `sync_repository`, or the next cidx write's enqueue.

## Operator tooling

Host-side tools that build or use the repository volume's topology refuse under
`content_backend: delegated`, before touching the disk:

| Tool | Under delegated |
|---|---|
| Auto-updater CoW-daemon symlink steps (`data/golden-repos`, `data/activated-repos` into the CoW mount) | With `clone_backend: delegated` (the supported setup): unchanged no-op. With `clone_backend: cow-daemon`: refused, `DEPLOY-GENERAL-226` names why, nothing is created or renamed. The updater reads `content_backend` from the config it loads; it runs outside the server. |
| `cidx global activate` | Exit 1 with the reason: the alias it registers names this host's `.code-indexer/index`, which no other pod has. Add or refresh through the server instead. Reads the server config at `CIDX_SERVER_DATA_DIR` (default `~/.cidx-server`); no config = plain CLI host, unchanged; an unreadable one is refused. |

## Deployment

* `content_backend: delegated`, `clone_backend: delegated` (`workspace_mcp.endpoint`, `api_key` = `WSM_CLONE_SHARED_SECRET`), `git_backend: delegated`.
* workspace-mcp with the clone-path `stat`/`delete`/`if_absent` API (workspace-mcp MR !63); `WSM_CLONE_ALLOWED_ROOTS` covering the golden-repos AND activated-repos roots.
* git-mcp must see the activated-repos root at the same path (its `cow_path`).
* File tools on activated copies additionally need `WSM_CONTENT_ALLOWED_ROOTS` and `WSM_CONTENT_WRITE_ROOTS` to cover the activated-repos root.

## Tests

`tests/unit/server/repositories/test_activated_repos_content_seam.py` (and
`test_activated_composite_content_seam.py` for composites) runs each
step locally and delegated over one upstream repository (fakes in
`tests/unit/server/activated_seam_fakes.py`) and requires equal results, with
every local access to a pod repository path -- and any subprocess run in one --
raising, except the pod's own `.code-indexer`.
