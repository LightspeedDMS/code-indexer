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
| Activate: index | `.code-indexer` comes with the clone | golden repo's index on THIS pod (checked out from the IndexStore first when another pod published it) copied to the copy's index root (pod state), then `cidx fix-config`; with `index_store_backend: delegated` the copy's index is then published and its version recorded in the registry entry |
| Exists? | `os.path.exists` | `POST /clone-path/stat` |
| Branch list / switch / sync status / conflicts | raw git + git seam | git seam only (`list_branches`, `log_raw`, `rev_parse`, `status_porcelain`) |
| Deactivate | drain, rename to `.trash`, purge | drain, `POST /clone-path/delete`, delete the copy's IndexStore alias (`/index/delete`), drop pod index state, THEN the registration (a failed delete keeps it: deactivating again finishes) |
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

## The copy's index on every pod (`index_store_backend: delegated`)

The copy's index is pod state, built where it was activated or reindexed. It is
published to the IndexStore under `activated.<sha256(username, user_alias)>`
(derived server-side from the registry pair, never a golden alias; an entry
naming another alias is refused) and the version is recorded in the registry
entry (`index_store_alias`, `index_version`), as for golden repos
([index-store-cross-pod.md](index-store-cross-pod.md)).

| Step | Delegated IndexStore |
|---|---|
| Query (semantic/FTS/hybrid), `/api/query` FTS check, index status, health | `ActivatedRepoManager.serve_index_here`: registry gate for THIS user, then this pod holds the recorded version (checked out, or an older one swapped by rename) |
| `trigger_reindex` | presence = registry; the job serves first, indexes, then publishes on top of the version it was built from (CAS: a publish by another pod in between fails the job) |
| Branch switch / sync delta reindex | serve, delta index, publish |
| HNSW orphan sweep | serve, repair, publish the repair |
| Deactivate | delete the alias |

A composite publishes its proxy config under its own alias and each member's
index under `activated.<sha256(username, "<composite>/<member>")>`; the entry
records `index_version` (config) and `member_index_versions` (members from the
registry's `discovered_repos`, never the pod's disk). Serving checks all of
them out, so a composite is routed as a composite on any pod; deactivation
deletes every alias.

A failed fetch or publish is an error (job failed with the cause / query
error), never "not found". Local store: none of this runs.

## Deployment

* `content_backend: delegated`, `clone_backend: delegated` (`workspace_mcp.endpoint`, `api_key` = `WSM_CLONE_SHARED_SECRET`), `git_backend: delegated`.
* workspace-mcp with the clone-path `stat`/`delete`/`if_absent` API (workspace-mcp MR !63) and `/index/delete` (!66); `WSM_CLONE_ALLOWED_ROOTS` covering the golden-repos AND activated-repos roots.
* `index_store_backend: delegated` with cidx-content-backend >= 0.9.0 (`delete_alias`; the image pins 0.9.0).
* git-mcp must see the activated-repos root at the same path (its `cow_path`).
* File tools on activated copies additionally need `WSM_CONTENT_ALLOWED_ROOTS` and `WSM_CONTENT_WRITE_ROOTS` to cover the activated-repos root.

## Tests

`tests/unit/server/repositories/test_activated_repos_content_seam.py` (and
`test_activated_composite_content_seam.py` for composites) runs each
step locally and delegated over one upstream repository (fakes in
`tests/unit/server/activated_seam_fakes.py`) and requires equal results, with
every local access to a pod repository path -- and any subprocess run in one --
raising, except the pod's own `.code-indexer`.
