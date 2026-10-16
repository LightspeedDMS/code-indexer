# Operating modes

CIDX runs in one of three ways on a workstation (CLI, daemon, or as a client of a server), and as a multi-user
server, optionally clustered. This page explains what each mode does and how to switch, so you can choose one.

| Mode | Where the index lives | Who uses it | Set up with |
|------|----------------------|-------------|-------------|
| CLI | `.code-indexer/` in the project | one developer | `cidx init` |
| Daemon | same as CLI, plus an in-memory cache in a background process | one developer | `cidx config --daemon` |
| Remote client | on a CIDX server | one developer, against a shared server | `cidx init --remote <url> --username ... --password ...` |
| Server | `~/.cidx-server/` on the server host | a team, through REST, MCP and the Web UI | [Server deployment](../server/deployment.md) |
| Cluster | shared storage plus PostgreSQL | a team, several server nodes | [Cluster setup](../server/cluster-setup.md) |

Every mode needs an embedding provider key for semantic search (see [Configuration](configuration.md)). Nothing
runs in a container.

## CLI mode

The default. Each `cidx` command runs in its own process and loads the index from disk, then exits.

```bash
cidx init
cidx index --fts
cidx query "retry with backoff" --limit 5
cidx status
```

Per-project files under `.code-indexer/`: `config.json`, `index/<collection>/` (one collection per embedding
model, for example `voyage-code-3`), `tantivy_index/` (full-text index, when built) and the SCIP index when
generated. `cidx uninstall --confirm` removes `.code-indexer/` and all of the project's index data.

Every semantic query calls the embedding provider to embed the query text, in every mode. What differs between
modes is only whether the index has to be loaded from disk first.

## Daemon mode

A background process per project keeps the HNSW and full-text indexes in memory, so queries skip the load from
disk. The daemon also runs watch mode.

```bash
cidx config --daemon    # enable for this project (stored in .code-indexer/config.json)
cidx start              # start now; otherwise it starts on the first query
cidx status             # "Daemon Mode: Active", with the socket name and cache TTL
cidx query "retry with backoff"   # served by the daemon
cidx stop               # stop the daemon
cidx config --no-daemon # back to CLI mode
```

- The daemon listens on a Unix socket at `/tmp/cidx/<hash>.sock`, where `<hash>` is derived from the project path.
  Its log is `.code-indexer/daemon.log`.
- An idle cached index is dropped after the cache TTL: 10 minutes by default, changed with
  `cidx config --daemon-ttl <minutes>` or `cidx init --daemon-ttl <minutes>`.
- The daemon serves one user on one machine; the socket is local.

### Watch mode

```bash
cidx watch              # start watching (returns at once; the daemon keeps watching)
cidx watch --debounce 3.0   # wait 3 s after the last change before re-indexing (default 2.0)
cidx watch-stop         # stop watching
```

In daemon mode, `cidx watch` hands the watcher to the daemon and returns immediately. The watcher follows file
changes and branch switches and re-indexes the changed files. Run `cidx index` once before starting it.

## Remote client mode

`cidx init --remote https://cidx.example.com --username alice --password ...` turns the project directory into a
client of a CIDX server: queries and the remote command groups (`repos`, `jobs`, `admin`, `files`, `git`, ...) go to
the server's REST API instead of a local index. `cidx --help` marks which commands are available in which mode.

## Server mode

The CIDX server is a separate deployment for a team. It manages golden repositories (shared, centrally indexed
clones), lets users activate their own copies, and serves:

- a REST API (for example `POST /api/query`),
- an MCP endpoint at `/mcp` for AI assistants (see [MCP registration](mcp-registration.md)),
- a Web UI for administration, under `/admin`.

The server keeps its data in `~/.cidx-server/` (or `CIDX_SERVER_DATA_DIR`). Only bootstrap settings are in
`~/.cidx-server/config.json`; all other settings are runtime settings in the server database, changed in the Web
UI configuration screen. Install and operation: [Server deployment](../server/deployment.md). Accounts, SSO and
MFA: [Login and elevation](../server/auth/login-and-elevation.md) and [OIDC](../server/auth/oidc.md).

### Cluster mode

With `storage_mode` set to `"postgres"` (a bootstrap key in `config.json`), several server nodes share one
PostgreSQL database for state and coordination, and any node can serve any request. See
[Cluster setup](../server/cluster-setup.md) and [Cluster architecture](../architecture/cluster.md).

### Research Assistant

The server's Web UI has a Research Assistant at `/admin/research`: a chat in which an admin directs a Claude CLI
session to investigate and repair server problems. It is not a read-only investigation tool; it has remediation
authority over the server's own environment and data.

- **Access**: admin Web UI session only. Sending a message requires TOTP elevation when elevation enforcement is
  on (`require_elevation()` in `src/code_indexer/server/routers/research_assistant.py`).
- **Remediation authority**: the Claude CLI session gets the Bash, Read, Glob, Grep, Write, Edit and TodoWrite
  tools, with permission rules passed through `--settings`. File operations (`rm`, `mv`, `cp`, `mkdir`, `rmdir`,
  `touch`, `chmod`, `chown`, `ln`) and `kill`/`pkill` are deliberately left allowed so it can repair corrupted
  indexes, orphaned metadata or stuck jobs. Denied: privilege escalation, interpreters and nested shells, package
  managers, service start/stop, git writes, `killall`, and network tools (`_bash_deny_rules` and
  `_build_permission_settings` in `src/code_indexer/server/services/research_assistant_service.py`).
- **Edit scope**: the permission settings declare a tool-level deny for Write and Edit together with an Edit allow
  rule scoped to the cidx-meta directory (repository descriptions and dependency maps). Which of the two the
  Claude CLI applies to an edit inside cidx-meta depends on its rule precedence and is not documented here.
- **Network scope**: direct `curl` is denied; HTTP requests go through `scripts/cidx-curl.sh`, which allows only
  loopback plus operator-configured CIDR ranges.
- **Audit**: every message of every session is stored in the `research_messages` table.

## Switching modes

| From | To | Steps |
|------|----|-------|
| CLI | daemon | `cidx config --daemon`, then `cidx start` (optional) |
| daemon | CLI | `cidx stop`, then `cidx config --no-daemon` |
| local (CLI or daemon) | server | Register the repository on the server as a golden repository; the local `.code-indexer/` can be removed with `cidx uninstall`. |
| server | local | Clone the repository and run `cidx init` and `cidx index` in it. |

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `cidx status` shows `Daemon Mode: Configured` (enabled but stopped) | Run `cidx start`, or just query: the daemon starts on the first query. |
| Daemon does not start | Read `.code-indexer/daemon.log`. |
| `cidx watch` prints `No indexes found. Run 'cidx index' first.` | Run `cidx index` first. Outside daemon mode the standalone watcher's index detection does not recognise the current index layout; enable daemon mode and run `cidx watch` again. |
| Queries still slow in daemon mode | The first query after start or after the TTL expired loads the index. Semantic queries always include the embedding call. |
