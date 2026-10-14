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

Refused under delegated (fail loud): composite activation; a bare golden clone;
a `clone_backend` other than `delegated`; `git_backend: local`. An unreachable
workspace-mcp raises -- it is never "repository not found".

## Deployment

* `content_backend: delegated`, `clone_backend: delegated` (`workspace_mcp.endpoint`, `api_key` = `WSM_CLONE_SHARED_SECRET`), `git_backend: delegated`.
* workspace-mcp with the clone-path `stat`/`delete`/`if_absent` API (workspace-mcp MR !63); `WSM_CLONE_ALLOWED_ROOTS` covering the golden-repos AND activated-repos roots.
* git-mcp must see the activated-repos root at the same path (its `cow_path`).
* File tools on activated copies additionally need `WSM_CONTENT_ALLOWED_ROOTS` and `WSM_CONTENT_WRITE_ROOTS` to cover the activated-repos root.

## Tests

`tests/unit/server/repositories/test_activated_repos_content_seam.py` runs each
step locally and delegated over one upstream repository (fakes in
`tests/unit/server/activated_seam_fakes.py`) and requires equal results, with
every local access to a pod repository path -- and any subprocess run in one --
raising, except the pod's own `.code-indexer`.
