# CIDX Server Deployment

This guide installs a single CIDX Server node (standalone mode, SQLite storage) on Linux with systemd. It covers
what the installer does, the files and units it creates, the bootstrap `config.json`, the first start, and the
seeded administrator account.

Related guides:

- Multi-node cluster (PostgreSQL, shared storage): [Cluster Setup](cluster-setup.md)
- Upgrading an installed node: [Upgrading](upgrading.md)
- The auto-updater the installer provisions: [Auto-Update](auto-update.md)
- Health endpoints, logs and metrics: [Observability](observability.md)

## Requirements

- Linux with systemd. The installer detects `dnf`, `yum` or `apt-get` (Rocky Linux, RHEL, Ubuntu).
- A regular user account with `sudo` rights. The installer refuses to run as root and calls `sudo` only for
  package installs, `/etc/systemd/system` writes and `systemctl`.
- Python 3.9 to 3.12 (`requires-python = ">=3.9,<3.13"` in `pyproject.toml`).
- A C/C++ compiler (`gcc`, `g++`): the custom hnswlib fork is compiled at install time. The installer installs it.
- Outbound HTTPS to:
  - your embedding provider (VoyageAI or Cohere);
  - GitHub: the repository, the hnswlib fork, pace-maker, and ripgrep release downloads;
  - the Python package index (`pip install`);
  - and, for the first auto-update deployment (see [Auto-Update](auto-update.md#deployment-steps)):
    `nodejs.org` (pinned Node.js tarball), `sh.rustup.rs` plus the Rust toolchain servers rustup downloads from
    and the crates registry used by `cargo` to build `xray-cli`, the npm registry (Claude CLI, Codex CLI,
    scip-python), and `claude.ai/install.sh` (Claude CLI installer). The Rust toolchain and `xray-cli` build step
    is fatal: without that access the deployment stops and the server is not restarted.

## Install

The installer is `scripts/install-cidx-server.sh` in this repository. It is idempotent: re-running it on an
installed node pulls the tracked branch and re-applies each step.

Preview every action first. `--dry-run` prints the package installs, clone, pip commands, `config.json`, systemd
units and service restart without executing any of them:

```bash
bash scripts/install-cidx-server.sh --dry-run
```

Then install:

```bash
bash scripts/install-cidx-server.sh --branch master --port 8000 --voyage-key <voyage-api-key>
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--branch` | `master` | Branch to clone and, unless `--auto-update-branch` is given, the branch the auto-updater tracks |
| `--port` | `8000` | Port written into the `cidx-server` unit's `ExecStart` and the initial `config.json` |
| `--voyage-key` | none | Written into the unit as `Environment="VOYAGE_API_KEY=..."` (optional, see [Embedding provider keys](#embedding-provider-keys)) |
| `--install-dir` | `~/code-indexer` | Where the repository is cloned |
| `--repo-url` | the public GitHub repository | Repository to clone |
| `--repo-token` | none | Token for a private repository, stored in `~/.git-credentials` (mode 600), never in the remote URL |
| `--auto-update-branch` | value of `--branch` | Branch the auto-updater tracks (`CIDX_AUTO_UPDATE_BRANCH`) |
| `--workers` | `1` | uvicorn worker count in `ExecStart` |
| `--dry-run` | off | Print what would happen, change nothing |

Cluster flags (`--node-id`, `--postgres-dsn`, `--clone-backend`, `--cow-daemon-*`, `--nfs-*`, `--cow-local-bind`)
are covered in [Cluster Setup](cluster-setup.md). Run `bash scripts/install-cidx-server.sh --help` for the full list.

### What the installer does (standalone)

In order, as printed by `--dry-run`:

1. Installs system packages: `git nfs-utils gcc gcc-c++ python3-pip python3-devel jq` (dnf/yum, after enabling
   EPEL and CRB) or `git nfs-common gcc g++ python3-pip python3-dev libpq-dev jq` (apt).
2. Clones the repository into `--install-dir` (or fetches, checks out and pulls the branch if already cloned) and
   initializes the `third_party/hnswlib` submodule.
3. Runs `python3 -m pip install --break-system-packages -e .` and installs `psycopg[binary] psycopg-pool requests numpy`.
4. Creates `~/.cidx-server/data/golden-repos`, `~/.cidx-server/logs` and `~/.cidx-server/locks`, and writes a
   default `~/.cidx-server/config.json` only if none exists.
5. Clones and installs pace-maker into `~/claude-pace-maker`; on a fresh install it switches pace-maker's master
   switch off and records `pace_maker_clone_path` in `config.json` (see [Auto-Update](auto-update.md#pace-maker)).
6. Writes `/etc/systemd/system/cidx-server.service` and enables it.
7. Adds a `safe.directory = *` entry to the service user's global git config.
8. Installs `cidx-auto-update.service` and `cidx-auto-update.timer` from
   `src/code_indexer/server/auto_update/templates/` and starts the timer.
9. Restarts `cidx-server` and polls `GET http://localhost:<port>/docs` until it returns HTTP 200 (up to 30 seconds).

The installer opens a firewalld port only in cluster mode. On a standalone node open the port yourself if a
firewall is active:

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

## Installed layout

| Path | Contents |
|------|----------|
| `~/code-indexer/` | Repository checkout the server runs from (`WorkingDirectory`, `PYTHONPATH=<checkout>/src`) |
| `~/.cidx-server/config.json` | Bootstrap configuration (see below) |
| `~/.cidx-server/data/cidx_server.db` | Users, settings (runtime configuration row), jobs, golden-repo metadata |
| `~/.cidx-server/groups.db`, `oauth.db`, `refresh_tokens.db`, `scip_audit.db` | Groups and audit log, OAuth clients, refresh tokens, SCIP audit |
| `~/.cidx-server/logs.db` | Application log database (see [Observability](observability.md)) |
| `~/.cidx-server/.jwt_secret` | JWT signing secret (standalone mode) |
| `~/.cidx-server/.encryption_key_salt` | Needed to decrypt stored git credentials and CI tokens; back it up with the database |
| `~/.cidx-server/data/golden-repos/` | Golden repository clones and their indexes |
| `~/.cidx-server/data/activated-repos/` | Per-user activated repositories |
| `~/.cidx-server/launch.json`, `applied_launch.json` | Target and applied host/port/workers (written by the server and the auto-updater) |
| `~/claude-pace-maker/` | pace-maker checkout |

The server data directory is `~/.cidx-server` unless the `CIDX_SERVER_DATA_DIR` environment variable is set.

## systemd units

`/etc/systemd/system/cidx-server.service`, as written by the installer (port 8000, one worker):

```ini
[Service]
Type=simple
User=<install user>
WorkingDirectory=<install dir>
Environment="PATH=<home>/.cargo/bin:<home>/.local/bin:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"
Environment="PYTHONPATH=<install dir>/src"
Environment="CIDX_SERVER_MODE=1"
Environment="CIDX_ISSUER_URL=http://localhost:8000"
Environment="CIDX_REPO_ROOT=<install dir>"
Environment="CIDX_AUTO_UPDATE_BRANCH=master"
ExecStart=python3 -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000 --log-level info --workers 1
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cidx-server
```

`CIDX_ISSUER_URL` is the OAuth issuer the server advertises in its discovery documents
(`/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource`). If MCP or OAuth clients reach
the server through a public URL, set it to that URL, for example with a drop-in (`sudo systemctl edit cidx-server`):

```ini
[Service]
Environment="CIDX_ISSUER_URL=https://cidx.example.com"
```

The auto-updater manages parts of this unit on every deploy (the `--host`/`--port`/`--workers` flags of
`ExecStart`, `PATH` entries, `CIDX_REPO_ROOT`, `MALLOC_ARENA_MAX`); see [Auto-Update](auto-update.md). Do not edit
those by hand.

Service management:

```bash
sudo systemctl status cidx-server
sudo systemctl restart cidx-server
journalctl -u cidx-server -f
```

## Bootstrap configuration (config.json)

`~/.cidx-server/config.json` holds only the settings the server needs before its database is open. Everything else
is a runtime setting stored in the database and changed through the Web UI configuration screen (`/admin/config`).
The authoritative bootstrap list is `BOOTSTRAP_KEYS` in `src/code_indexer/server/services/config_service.py`:

| Key | Purpose |
|-----|---------|
| `server_dir` | Server data directory |
| `storage_mode` | `sqlite` (standalone, default) or `postgres` (cluster) |
| `postgres_dsn` | PostgreSQL connection string (cluster) |
| `cluster` | `node_id` (cluster node identity) and `sharding_enabled` |
| `clone_backend`, `cow_daemon`, `ontap` | Clone backend for versioned snapshots and activations (cluster storage) |
| `pace_maker_clone_path` | pace-maker checkout path, written by the installer and the auto-updater |
| `enable_malloc_arena_max`, `enable_malloc_trim` | glibc memory mitigations (both default `true`) |
| `server_threadpool_size`, `mcp_dispatch_pool_size`, `query_executor_pool_size` | Startup thread-pool sizes |
| `fault_injection_enabled`, `fault_injection_nonprod_ack` | Fault-injection harness gate (non-production only, see [Fault Injection](fault-injection.md)) |
| `enable_graph_channel_repair`, `graph_repair_*` | Dependency-map graph repair switches |
| `enable_predeactivation_leak_scan`, `orphan_trash_sweep_per_startup_cap` | Startup cleanup controls |

Bootstrap keys are read at startup; restart the service after changing one.

Every settings save in the Web UI rewrites `config.json` from the bootstrap values the running process loaded at
its start. A hand edit made after the server started is therefore overwritten by the next save. Edit
`config.json`, then restart the server promptly, without saving any settings in between.

`host`, `port`, `workers` and `log_level` are not bootstrap keys. The installer writes them into the initial
`config.json` only as first-boot seeds. On the first start the server copies every non-bootstrap key into the
runtime configuration row in the database (filling `host`, `port` and `workers` from the unit's `ExecStart` when
`config.json` lacks them), then rewrites `config.json` to bootstrap keys only. The original file is kept as
`~/.cidx-server/config-migration-backup/config.json.pre-centralization`. From then on change these four values in
the Web UI (Configuration, Server section). Saving them restarts nothing:

- `host`, `port` and `workers` are flags of the unit's `ExecStart`. They change only when a restart is requested
  through the Web UI (`POST /admin/restart`, the restart action) or, in cluster mode, by the cluster-wide restart
  generation bump that action makes. The auto-updater then rewrites `ExecStart` and restarts the server. A manual
  `systemctl restart cidx-server` or a routine code deployment keeps the current flags.
- `log_level` is read from `~/.cidx-server/launch.json`, which every settings save rewrites, so it applies at the
  next restart of any kind. `launch.json` is per node: in cluster mode the node that served the save rewrites its
  own file at once, and every other node rewrites its file when it next re-reads the shared configuration (every
  30 seconds).

See [Auto-Update](auto-update.md#restart-requests-and-launch-settings).

## Embedding provider keys

The server needs a VoyageAI or Cohere API key to index and query. Two sources exist:

- Web UI, Configuration, Provider API Keys. A key stored there is exported to the server process environment at
  startup and takes precedence.
- The process environment, for example `VOYAGE_API_KEY` written into the unit by `--voyage-key`. Used when the Web
  UI field is empty.

## First start and the administrator account

When no account named `admin` exists at startup, the server creates one with the `admin` role and a default
password defined in `UserManager.seed_initial_admin` (`src/code_indexer/server/auth/user_manager.py`). Treat a
server that still accepts that password as unauthenticated: the account can register golden repositories, read
every repository and manage users.

Before the server is reachable from any untrusted network:

1. Log in to the Web UI at `http://<host>:8000/admin` as `admin`.
2. Change the password in the Web UI user settings, or through the REST API:

   ```bash
   TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username": "admin", "password": "<current password>"}' | jq -r .access_token)

   curl -s -X PUT http://localhost:8000/api/users/change-password \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"old_password": "<current password>", "new_password": "<new password>"}'
   ```

3. Confirm the old password no longer authenticates.

Change the password of the `admin` account rather than deleting the account. Enabling TOTP MFA on administrator
accounts adds step-up elevation for sensitive operations; see [Login and Elevation](auth/login-and-elevation.md).

## Verify the installation

```bash
systemctl status cidx-server
systemctl list-timers cidx-auto-update.timer
curl -s http://localhost:8000/healthz
```

`/healthz` is unauthenticated and returns `{"status": "healthy"}` (HTTP 200), `{"status": "degraded"}` (HTTP 200)
or `{"status": "unhealthy"}` (HTTP 503). See [Observability](observability.md) for what each status means.

## Ports and network

One port (default 8000) serves everything: REST API, MCP (`/mcp`), the Web UI (`/admin`), OAuth endpoints and the
OpenAPI page (`/docs`). The default comes from the installer (`PORT=8000`) and the `ServerConfig.port` default.

To serve TLS, terminate it at a reverse proxy and forward to the node:

```nginx
server {
    listen 443 ssl;
    server_name cidx.example.com;

    ssl_certificate     /etc/ssl/certs/cidx.crt;
    ssl_certificate_key /etc/ssl/private/cidx.key;

    # Maintenance-mode switches are for the local auto-updater only.
    location = /api/admin/maintenance/enter { return 403; }
    location = /api/admin/maintenance/exit  { return 403; }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

The two maintenance endpoints accept only loopback peers (`require_localhost`). A proxy on the same host connects
from loopback, so the proxy itself must refuse those paths; see [Maintenance and Jobs](maintenance-and-jobs.md).
When the proxy publishes a public URL, set `CIDX_ISSUER_URL` to it (see [systemd units](#systemd-units)).

## Backups

Stop the service (or use `sqlite3 <db> ".backup <dest>"` per database) for a consistent copy of:

- `~/.cidx-server/config.json` and `~/.cidx-server/.jwt_secret`
- `~/.cidx-server/.encryption_key_salt`, together with the database. A restore without this file cannot decrypt
  the stored git credentials and CI tokens.
- `~/.cidx-server/data/cidx_server.db`
- `~/.cidx-server/groups.db`, `oauth.db`, `refresh_tokens.db`, `scip_audit.db`

`~/.cidx-server/data/golden-repos/` can be rebuilt by re-registering and re-indexing the repositories, at the cost
of the indexing time and embedding-provider calls; back it up if that cost matters.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Installer ends with `Health check FAIL: GET http://localhost:<port>/docs did not return 200` | `journalctl -u cidx-server --no-pager -n 30` |
| `No module named 'code_indexer'` in the journal | The unit's `PYTHONPATH` must point at `<install dir>/src` |
| Service exits immediately | Port already in use: `ss -ltnp | grep 8000` |
| A changed host, port or worker count in the Web UI never applies | No restart was requested through the Web UI restart action (a manual `systemctl restart` does not apply them), or the auto-update timer is not running: `systemctl status cidx-auto-update.timer` |
| Startup log ERROR about hnswlib missing `check_integrity()`/`repair_orphans()` | [Custom hnswlib Build](hnswlib-custom-build.md) |
| `/healthz` returns 503 | `GET /api/system/health` (authenticated) lists `failure_reasons`; see [Observability](observability.md) |
