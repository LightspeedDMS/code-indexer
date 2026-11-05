# Workspace registration sync

cidx registers agent workspaces as `ws-<8 hex>` golden repositories so that
agents can search their own workspace. It also removes them when the
workspace is destroyed. The code is in
`src/code_indexer/server/services/workspace_registration_sync.py`.

## Configuration

| Setting | Meaning |
|---------|---------|
| `CIDX_WORKSPACE_SYNC_MODE` | `off` (default), `dry-run` or `apply` |
| `CIDX_WORKSPACE_SYNC_WORKSPACE_MCP_URL` | Base URL of the workspace-mcp that provisions the agent workspaces and serves `GET /workspaces` |
| `CIDX_WORKSPACE_MCP_WORKSPACES_TOKEN` / `..._TOKEN_FILE` | That service's listing bearer: the value, or a file that holds it |
| `CIDX_WORKSPACE_SYNC_INTERVAL_SECONDS` | Interval between scheduled passes (default 300) |
| `CIDX_WORKSPACE_SYNC_MAX_DEREGISTER_PER_PASS` | Most deregistrations in one pass (default 20) |
| `CIDX_WORKSPACE_SYNC_DESTROYED_LOOKBACK_DAYS` | How far back to list destroyed workspaces (default 30) |

The listing URL is separate from `workspace_mcp.endpoint` on purpose. That
key names the workspace-mcp that cidx reads repository content through (the
content seam and the delegated clone backend). That service holds no agent
workspaces. The sync never falls back to it.

The sync only reports, and logs the reason, when any of these is true:

- the listing URL is unset;
- the listing token is unset;
- the listing service has no `GET /workspaces` (404).

A refusal (401 or 503), or any other failure, is an error, and nothing is
deregistered.

## Which workspaces can be registered

`GET /workspaces` returns each workspace's `mount_path`. That is a path on
the provisioning service's own volume. Registering a workspace runs cidx's
normal add path with `file://<mount_path>`, so cidx must be able to read
that path:

- **Local content backend:** the path must exist on the cidx pod. The add
  copies it.
- **Delegated content backend:** cidx reads repositories only through its
  content seam (workspace-mcp `/content/*`, confined to its allowed roots,
  the golden-repos and activated-repos directories). It also runs git only
  through its git seam, at the golden repository's own path. The add
  therefore accepts a local source only in place, at
  `<golden_repos_dir>/<id>`.

A workspace that cannot be read is listed under `unregistrable` in the pass
result, with the reason. No add job is submitted for it.

### Limitation: out-of-place workspaces under delegation

A workspace provisioned on its own volume, not in place under cidx's
golden-repos directory, is not readable by cidx under delegation. There are
three separate gaps:

1. **Content:** cidx's content workspace-mcp does not mount the
   provisioning volume, and its allowed roots do not include it.
2. **Git:** the git seam's git-mcp sees only the golden-repos volume. The
   add path's `ls-remote` and branch lookups, and later refreshes, run there.
3. **Registration:** a delegated add refuses a `file://` source that is not
   already at the golden repository's path.

### Proposed design: per-repository content source

The smallest change that makes these workspaces indexable, without copying
them:

1. **Provisioning service:** it exposes its own workspace volume through the
   same `/content/manifest`, `/content/fetch` and `/content/rg` endpoints,
   with its workspace root as an allowed root. It also answers git reads for
   that volume, either through a git-mcp that mounts it or through
   `git_exec` on the same service.
2. **cidx registry:** a golden repository record carries an optional content
   source: the base URL of the service that holds its files, and the root
   path there. A `ws-*` registration records the provisioning service's URL
   and the workspace's `mount_path`. The record's `clone_path` stays
   `<golden_repos_dir>/<id>` and holds only the index state, as for every
   delegated repository.
3. **cidx seams:** `content_seam_source`, `server_regex_target` and the
   golden git reader resolve a repository with a recorded content source to
   that service and root. Every other repository keeps today's single
   content backend.
4. **Registration:** under delegation, a `ws-*` add with a recorded content
   source skips the in-place check, and waits for the clone to be visible
   through the recorded source instead of the default one.

Until that exists, the supported setup is to provision workspaces in place
under cidx's golden-repos directory. The provisioning service can do this by
writing into the shared golden-repos volume. The sync registers exactly
those.
