# CoW Storage Daemon Setup

This guide configures CIDX cluster nodes to use the CoW Storage Daemon (`clone_backend: cow-daemon`) as shared
storage. The daemon runs on one host with a reflink-capable filesystem, creates copy-on-write clones through a
REST API, and exports its storage over NFS so every CIDX node sees the same files.

General cluster procedure (PostgreSQL, node identity, load balancer): [Cluster Setup](cluster-setup.md).
Architecture: [Cluster Architecture](../architecture/cluster.md).

## Storage layout

This is the only layout the installer (`scripts/install-cidx-server.sh`) creates and the auto-updater
(`DeploymentExecutor` in `src/code_indexer/server/auto_update/deployment_executor.py`) maintains:

```
CoW daemon host
  <base_path>/                 reflink-capable filesystem (XFS reflink=1 or btrfs), NFS-exported
    golden-repos/              golden repository clones and indexes
    activated-repos/           per-user activations
    cidx/ ...                  clones the daemon creates (namespaces)

Every CIDX node
  /mnt/cow-storage             ONE mount of <base_path>:
                                 - NFS client nodes: nfs, _netdev,vers=3,nolock,soft,timeo=30,retrans=3
                                 - the daemon host, if it also runs CIDX: bind mount of <base_path>
  ~/.cidx-server/data/golden-repos     -> /mnt/cow-storage/golden-repos     (symlink)
  ~/.cidx-server/data/activated-repos  -> /mnt/cow-storage/activated-repos  (symlink)
```

Rules that follow from the code:

- **There is one shared mount per node.** Golden repositories are not mounted separately. The server derives the
  golden-repos directory as `<server_dir>/data/golden-repos` with no setting to move it, so that path must be a
  symlink into the CoW mount.
- **Golden repositories and activations must live on the daemon's filesystem.** Per-user activation asks the
  daemon to `cp --reflink` a golden repository into `activated-repos/<user>/`; reflink requires source and target on
  the same filesystem. `CowDaemonBackend._translate_to_daemon_path` resolves each path with `os.path.realpath()`
  and accepts it only under `cow_daemon.mount_point` or `cow_daemon.daemon_storage_path`. A directory that is
  itself a mount point (a bind or NFS mount placed directly at `~/.cidx-server/data/golden-repos`) resolves to its
  own path and fails with `... cannot translate to daemon view`.
- **Symlinks always target the `mount_point` form**, on every node including the daemon host
  (`_resolve_golden_repos_symlink_target`, installer `_resolve_cow_symlink_target`).
- **NFS is version 3 with `nolock`.** The installer writes `vers=3,nolock`; the auto-updater's
  `_ensure_cow_storage_mount_options()` rewrites any `/etc/fstab` entry for `cow_daemon.mount_point` that lacks
  them (it changes the type to `nfs` and replaces any `vers=` option). NFSv4 is not supported: it handles locking
  inside the protocol, so `nolock` has no effect there.
- **The mount is `soft,timeo=30,retrans=3`.** That is what the installer sets (`add_fstab_entry`,
  `setup_nfs_mount`). The auto-updater's rewrite keeps whatever `soft`/`hard` option the entry already has. A
  `soft` mount returns an I/O error after its retries instead of blocking a process indefinitely when the daemon
  host is unreachable.

The daemon host is a single point of failure: when it is down, every node loses golden repositories and
activations. `/healthz` on each node then reports `unhealthy` (HTTP 503) through its golden-repos readability
probe (see [Observability](observability.md)).

## Prerequisites

Daemon host:

- A reflink-capable filesystem for `base_path`. Check:

  ```bash
  echo test > /srv/cow-storage/.reflink-src
  cp --reflink=always /srv/cow-storage/.reflink-src /srv/cow-storage/.reflink-dst && echo SUPPORTED
  rm -f /srv/cow-storage/.reflink-src /srv/cow-storage/.reflink-dst
  xfs_info /srv/cow-storage | grep reflink    # XFS: expect reflink=1
  ```

- An NFS server, and the firewall open from the CIDX nodes for NFS (2049, plus rpcbind 111 and mountd for NFSv3)
  and the daemon API port (default 8081).

CIDX nodes: NFS client packages (the installer installs `nfs-utils` or `nfs-common`) and network access to the
daemon host on those ports.

## Step 1: Install the daemon

The daemon lives in its own repository (cow-storage-daemon). On the daemon host:

```bash
git clone <cow-storage-daemon repository URL> cow-storage-daemon
cd cow-storage-daemon
./scripts/install-cow-daemon.sh --storage-path /srv/cow-storage --service-group <cidx service user's primary group>
```

The installer also accepts `--port` (default 8081), `--api-key` (generated when omitted) and `--dry-run`. It
writes `/etc/cow-storage-daemon/config.json` and a `cow-storage-daemon` systemd unit.

Daemon configuration fields (`DaemonConfig`): `base_path` (required), `api_key` (required), `db_path` (default
`<base_path>/.cow-daemon.db`), `health_requires_auth` (default `false`), `allowed_source_roots` (default empty,
meaning any source path); the daemon also reads `port` (default 8081), `host`, and `service_group`. Environment
variables with the `COW_DAEMON_` prefix override the file.

Verify:

```bash
sudo systemctl status cow-storage-daemon
curl -s http://localhost:8081/api/v1/health
```

The health response carries `status`, `version`, `filesystem_type`, `cow_method`, disk usage and `uptime_seconds`.
CIDX refuses to start against a daemon whose reported `version` is missing or older than `0.2.0`.

### Service group

Two OS users write into each per-user directory `activated-repos/<user>/`: cidx-server (creates it, mode `2775`,
group = the service user's primary group) and the daemon (creates and removes the clone `<user>/<alias>` through
group permissions). Therefore:

- The daemon's `service_group` must be exactly the cidx service user's primary group.
- The daemon's OS user must be a member of that group, and the running daemon must have started after the
  membership existed.

On the daemon host (the node where `/etc/cow-storage-daemon/config.json` exists), both the installer
(`ensure_cow_daemon_service_group_membership`) and the auto-updater (`_ensure_cow_daemon_user_in_service_group`)
add the daemon user to the group when missing and restart `cow-storage-daemon` when its running process lacks the
group. The installer stops with an error on an unreadable config, a missing or unknown `service_group`, or a group
that differs from the service user's primary group; the auto-updater logs an ERROR and skips. On every other node
the step does nothing.

## Step 2: Export the storage over NFS

On the daemon host, export `base_path` to the cluster subnet (example uses the RFC 5737 documentation range):

```bash
echo '/srv/cow-storage  192.0.2.0/24(rw,async,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports
sudo exportfs -ra
showmount -e localhost
```

`async` acknowledges writes before they reach stable storage. It makes indexing over NFS much faster than `sync`
and is acceptable only where losing the last writes on a daemon-host crash is acceptable.

## Step 3: Install each CIDX node

Run the installer in cluster mode with the cow-daemon backend. It mounts the storage, creates both symlinks, and
writes the `cow_daemon` section of `config.json`. Preview with `--dry-run` first.

NFS client node:

```bash
bash scripts/install-cidx-server.sh \
  --branch master \
  --node-id node-2 \
  --postgres-dsn "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server" \
  --clone-backend cow-daemon \
  --cow-daemon-url "http://192.0.2.20:8081" \
  --cow-daemon-api-key <daemon api key> \
  --nfs-server 192.0.2.20 \
  --nfs-export /srv/cow-storage \
  --cow-daemon-storage-path /srv/cow-storage
```

Pass `--cow-daemon-storage-path` on every node, including the daemon host. It sets
`cow_daemon.daemon_storage_path`, the daemon's own `base_path`, which every clone request uses to translate a
`/mnt/cow-storage/...` path into the daemon's local path. Without the flag the installer falls back to the
`CIDX_COW_DAEMON_STORAGE_PATH` environment variable, then to the `base_path` in
`/etc/cow-storage-daemon/config.json`. That file exists only on the daemon host, and the daemon installer writes it
mode 600 for the daemon's own user, so it is used only when the cidx-server user can read it. When nothing
resolves the field stays unset (the installer prints a WARNING) and every CoW clone fails. The auto-updater
resolves the value from the same two sources, so it cannot repair such a node unless `CIDX_COW_DAEMON_STORAGE_PATH`
is set in the `cidx-auto-update` unit's environment.

The daemon host, if it also runs CIDX (it cannot NFS-mount its own export), adds `--cow-local-bind`; `--nfs-export`
is then the local source directory and `--nfs-server` is not needed. Keep `--cow-daemon-storage-path` there too.

`--nfs-mount` changes the mount point (default `/mnt/cow-storage`).

What the installer does for the storage:

1. Mounts the storage and adds the `/etc/fstab` line, then proves the mount is writable with a write/read/remove
   probe (and stops if it is not). On an NFS client node:

   ```
   192.0.2.20:/srv/cow-storage /mnt/cow-storage nfs _netdev,vers=3,nolock,soft,timeo=30,retrans=3 0 0
   ```

   With `--cow-local-bind` it is a bind mount instead (see
   [Bind Mount on the Daemon Host](#bind-mount-on-the-daemon-host)).

2. Creates the symlinks `~/.cidx-server/data/golden-repos -> /mnt/cow-storage/golden-repos` and
   `~/.cidx-server/data/activated-repos -> /mnt/cow-storage/activated-repos`. An empty existing directory is
   replaced; a non-empty one is moved to `<dir>.legacy.bug1337` (golden-repos) or `<dir>.legacy.bug1052`
   (activated-repos) and the symlink is created, with rollback if that fails; a symlink to another target is
   re-pointed. Other nodes that already migrated into the same shared directory are not affected.

3. Writes `config.json` (merged into any existing file, which is backed up first, mode 600):

   ```json
   {
     "host": "0.0.0.0",
     "port": 8000,
     "log_level": "INFO",
     "storage_mode": "postgres",
     "postgres_dsn": "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server",
     "workers": 1,
     "cluster": { "node_id": "node-2" },
     "clone_backend": "cow-daemon",
     "cow_daemon": {
       "daemon_url": "http://192.0.2.20:8081",
       "api_key": "<daemon api key>",
       "mount_point": "/mnt/cow-storage",
       "poll_interval_seconds": 2,
       "timeout_seconds": 600,
       "daemon_storage_path": "/srv/cow-storage"
     }
   }
   ```

   `host`, `port`, `log_level` and `workers` are first-boot seeds only; the server moves them into its runtime
   configuration on first start (see [Deployment](deployment.md#bootstrap-configuration-configjson)).

### Bind Mount on the Daemon Host

A CIDX node on the daemon host cannot NFS-mount its own export. With `--cow-local-bind` the installer bind-mounts
`--nfs-export` onto `--nfs-mount` and adds:

```
/srv/cow-storage  /mnt/cow-storage  none  bind  0  0
```

The node then sees the same files at the same `/mnt/cow-storage` path as every NFS client, and its symlinks point
into `/mnt/cow-storage` like every other node's. `df -T /mnt/cow-storage` shows the underlying filesystem (for
example `xfs`), not `nfs`, on that host.

### Bootstrap keys

`clone_backend` and `cow_daemon` are bootstrap keys: they are read at startup, and a change needs a restart. Use the
same `daemon_url`, `api_key`, `mount_point` and `daemon_storage_path` on every node.

| `cow_daemon` field | Default | Meaning |
|--------------------|---------|---------|
| `daemon_url` | `""` | Daemon REST base URL |
| `api_key` | `""` | Bearer token matching the daemon's `api_key` |
| `mount_point` | `""` | Where this node sees the daemon's storage (`/mnt/cow-storage`) |
| `daemon_storage_path` | unset | The daemon's `base_path`; used to translate `mount_point` paths into daemon-local paths |
| `poll_interval_seconds` | `2` | Initial poll interval while waiting for a clone job |
| `timeout_seconds` | `600` | Overall wait for one clone job |
| `request_timeout_seconds` | `30` | Timeout of each individual HTTP call to the daemon |

Nodes installed before these steps existed are repaired by the auto-updater on its next deployment (symlinks:
`_ensure_golden_repos_symlink_for_cow_daemon`, `_ensure_activated_repos_symlink_for_cow_daemon`; mount options:
`_ensure_cow_storage_mount_options`). `_ensure_daemon_storage_path` fills an empty storage path only when
`CIDX_COW_DAEMON_STORAGE_PATH` is set for the auto-updater or the daemon's config file is readable by the
cidx-server user (daemon host only); otherwise set it yourself. See
[Auto-Update](auto-update.md#deployment-steps).

## Step 4: Verify

On each node:

```bash
mount | grep /mnt/cow-storage                 # nfs with vers=3,nolock,soft (or a bind mount on the daemon host)
ls -l ~/.cidx-server/data/ | grep -- '->'     # golden-repos and activated-repos symlinks
journalctl -u cidx-server | grep -E "CoW daemon health check|NFS mount validation"
curl -s http://localhost:8000/healthz
```

At startup with `clone_backend: cow-daemon`, the server checks:

1. `GET <daemon_url>/api/v1/health` returns HTTP 200 within 10 seconds and reports version 0.2.0 or later
   (log: `CoW daemon health check: OK (version=...)`).
2. The mount point passes NFS validation (log: `NFS mount validation: OK (latency=...ms)`).

Either failure raises `RuntimeError` and the server does not start. There is no fallback backend.

Then register a golden repository and activate it as a regular user; the activation is a daemon clone under
`/mnt/cow-storage/activated-repos/<user>/`.

## Daemon REST API

All paths are under `/api/v1`; authentication is `Authorization: Bearer <api_key>`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health (unauthenticated when `health_requires_auth` is false) |
| GET | `/stats` | Storage statistics |
| POST | `/clones` | Submit a clone job; returns 202 with a `job_id` |
| GET | `/jobs/{job_id}` | Clone job status |
| GET | `/clones` | List clones |
| GET | `/clones/{namespace}/{name}` | One clone |
| DELETE | `/clones/{namespace}/{name}` | Delete a clone |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `RuntimeError: CoW daemon not reachable at ...` at startup | Daemon down, wrong `daemon_url`, or firewall | Start the daemon; check `daemon_url` and port 8081 |
| `CoW Daemon at ... is version ...; CIDX requires 0.2.0+` | Daemon too old | Upgrade the daemon |
| `RuntimeError: NFS mount is not healthy at ...` | Mount missing or not writable | `sudo mount -a`; check the export and permissions |
| Activation fails with `cannot translate to daemon view` | `golden-repos` or `activated-repos` is a real directory or its own mount, not a symlink into `mount_point` | Remove the separate mount; let the installer or the next auto-update create the symlink |
| Per-user activation AND versioned-snapshot publish both fail with `CoW daemon_storage_path is not configured` | `cow_daemon.daemon_storage_path` unset (typical on an NFS client node installed without `--cow-daemon-storage-path`) | Re-run the installer with the full flag set plus `--cow-daemon-storage-path <daemon base_path>`, or set the field in `config.json` and restart |
| New users cannot activate (`Permission denied`); existing users can | Daemon user not in `service_group`, or daemon not restarted since | See [Service group](#service-group); the next auto-update converges it |
| `TimeoutError: CoW daemon job ... did not complete within ...` | Clone slower than `timeout_seconds` | Check the daemon journal; raise `timeout_seconds` |
| Queries fail with `Input/output error` and `/healthz` is 503 | Daemon host or NFS unreachable (soft mount timed out) | Restore the daemon host; the mount recovers when the server answers |
| `Stale file handle` | Export changed or daemon host rebooted | `sudo umount -f /mnt/cow-storage && sudo mount -a` |
