# CoW Storage Daemon Setup for CIDX Clusters

This guide covers configuring a CIDX Server cluster to use the CoW Storage Daemon as its shared storage backend. The CoW daemon provides FlexClone-equivalent functionality using filesystem-level Copy-on-Write (reflinks), replacing the ONTAP/FSx dependency for non-production environments.

For general cluster setup (PostgreSQL, HAProxy, node joining), see [Cluster Setup Guide](cluster-setup.md). For the architecture explanation, see [Cluster Architecture Guide](cluster-architecture.md).

---

## Architecture Overview

The CoW Storage Daemon runs on a single host with a reflink-capable filesystem (XFS or btrfs). It exposes a REST API for clone lifecycle management (create, delete, list, inspect). NFS exports the storage directory so that all cluster nodes can read clone contents at the filesystem level.

```
                            +---------------------------+
                            |   CoW Daemon Host         |
                            |                           |
                            |  cow-storage-daemon:8081  |
                            |  (REST API for cloning)   |
                            |                           |
                            |  /srv/cow-storage/        |
                            |  (XFS reflink=1)          |
                            |  NFS-exported             |
                            +-----------+---------------+
                                        |
                       NFS mount: /mnt/cow-storage
                                        |
              +-------------------------+-------------------------+
              |                         |                         |
   +----------+----------+  +----------+----------+  +-----------+---------+
   |  CIDX Node 1        |  |  CIDX Node 2        |  |  CIDX Node 3        |
   |  clone_backend:      |  |  clone_backend:      |  |  clone_backend:      |
   |    "cow-daemon"      |  |    "cow-daemon"      |  |    "cow-daemon"      |
   |  mount: /mnt/cow-    |  |  mount: /mnt/cow-    |  |  mount: /mnt/cow-    |
   |    storage           |  |    storage           |  |    storage           |
   +----------------------+  +----------------------+  +----------------------+
```

**Two channels**: CIDX nodes send clone create/delete requests to the daemon's REST API. They read clone file contents via NFS from the shared mount point.

**Clone path resolution**: The daemon returns relative paths (e.g., `cidx/my-clone`). CIDX prepends the NFS mount point to get the absolute filesystem path (`/mnt/cow-storage/cidx/my-clone`).

---

## Prerequisites

### Daemon Host

- Linux with a **reflink-capable filesystem**: XFS formatted with `reflink=1`, or btrfs. The daemon hard-fails at startup if reflink is not supported -- no fallback.
- Python 3.9+
- Sufficient disk space for golden repo clones (each clone initially shares blocks with the source via reflink; disk usage grows only as files are modified)

Verify reflink support:

```bash
echo test > /tmp/reflink-src
cp --reflink=always /tmp/reflink-src /tmp/reflink-dst && echo "SUPPORTED" || echo "NOT SUPPORTED"
rm -f /tmp/reflink-src /tmp/reflink-dst
```

For XFS, verify the filesystem was formatted with reflink:

```bash
xfs_info /srv/cow-storage | grep reflink
# Expected: reflink=1
```

### CIDX Cluster Nodes

- A working CIDX Server cluster (see [Cluster Setup Guide](cluster-setup.md) for PostgreSQL, HAProxy, and node setup)
- NFS client packages (`nfs-utils` on Rocky/RHEL, `nfs-common` on Ubuntu)
- Network connectivity to the daemon host on ports 8081 (REST API) and 2049 (NFS)

---

## Step 1: Install the CoW Storage Daemon

On the host that will run the daemon (must have a reflink-capable filesystem):

```bash
# Clone the cow-storage-daemon repository
git clone <cow-storage-daemon-repo-url> cow-storage-daemon
cd cow-storage-daemon

# Run the production installer
./scripts/install-cow-daemon.sh --storage-path /srv/cow-storage [--port 8081] [--api-key YOUR_KEY]
```

The installer:

1. Validates reflink support on the storage filesystem
2. Installs system packages (python3, pip)
3. Installs Python dependencies from the repository
4. Generates an API key if not provided (save this -- shown only once)
5. Creates `/etc/cow-storage-daemon/config.json`
6. Creates and enables a systemd service (`cow-storage-daemon`)
7. Starts the daemon and validates health
8. Prints NFS export instructions

The script is idempotent and supports Rocky Linux, RHEL, and Ubuntu.

### Verify Installation

```bash
# Service status
sudo systemctl status cow-storage-daemon

# Health check
curl http://localhost:8081/api/v1/health
```

Expected health response:

```json
{
  "status": "healthy",
  "filesystem_type": "xfs",
  "cow_method": "reflink",
  "disk_total_bytes": 319026491392,
  "disk_used_bytes": 143545552896,
  "disk_available_bytes": 175480938496,
  "uptime_seconds": 42.5
}
```

### Daemon Configuration

The installer creates `/etc/cow-storage-daemon/config.json`:

```json
{
    "base_path": "/srv/cow-storage",
    "port": 8081,
    "api_key": "your-api-key",
    "health_requires_auth": false,
    "allowed_source_roots": []
}
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `base_path` | Yes | -- | Root directory for clone storage. Must be on a reflink-capable filesystem. |
| `api_key` | Yes | -- | Bearer token for API authentication. Restart daemon to change. |
| `port` | No | `8081` | TCP port to listen on. |
| `host` | No | `0.0.0.0` | Bind address. |
| `db_path` | No | `{base_path}/.cow-daemon.db` | Path to SQLite metadata database. |
| `health_requires_auth` | No | `false` | When `false`, `/api/v1/health` is unauthenticated (for load balancer probes). |
| `allowed_source_roots` | No | `[]` (allow all) | List of directory prefixes that clone source paths must be under. Empty allows any source. |

Settings can also be set via environment variables with the `COW_DAEMON_` prefix (e.g., `COW_DAEMON_BASE_PATH`), but the config file is recommended.

### Restricting Source Roots (Production)

For production clusters, restrict which directories can be cloned by setting `allowed_source_roots` to the golden repo base directory:

```json
{
    "base_path": "/srv/cow-storage",
    "api_key": "your-api-key",
    "allowed_source_roots": ["/home/cidx/.cidx-server/data/golden-repos"]
}
```

Clone requests with a `source_path` not under any listed root will be rejected with HTTP 400 `PATH_NOT_ALLOWED`.

---

## Step 2: Configure NFS

NFS provides filesystem-level access to clone contents. The daemon creates clones via its REST API; NFS makes the resulting directories visible to all cluster nodes.

### Server Side (Daemon Host)

**Install NFS server:**

```bash
# Rocky Linux / RHEL
sudo dnf install -y nfs-utils
sudo systemctl enable --now nfs-server

# Ubuntu / Debian
sudo apt install -y nfs-kernel-server
sudo systemctl enable --now nfs-kernel-server
```

**Export the storage directory:**

```bash
# Development/test -- open to all
echo '/srv/cow-storage  *(rw,sync,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports

# Production -- restrict to cluster subnet
echo '/srv/cow-storage  10.0.0.0/24(rw,sync,no_subtree_check,no_root_squash)' | sudo tee -a /etc/exports

# Apply and verify
sudo exportfs -ra
showmount -e localhost
```

**Open firewall ports:**

| Port | Protocol | Service |
|------|----------|---------|
| 2049 | TCP/UDP | NFS |
| 111 | TCP/UDP | rpcbind |
| 8081 | TCP | Daemon REST API |

```bash
# firewalld (Rocky/RHEL)
sudo firewall-cmd --permanent --add-service=nfs
sudo firewall-cmd --permanent --add-service=rpc-bind
sudo firewall-cmd --permanent --add-service=mountd
sudo firewall-cmd --permanent --add-port=8081/tcp
sudo firewall-cmd --reload

# ufw (Ubuntu)
sudo ufw allow from 10.0.0.0/24 to any port nfs
sudo ufw allow from 10.0.0.0/24 to any port 111
sudo ufw allow from 10.0.0.0/24 to any port 8081
```

### Client Side (Remote CIDX Nodes)

Every CIDX node that is NOT the daemon host needs an NFS mount:

```bash
# Install NFS client
sudo dnf install -y nfs-utils     # Rocky/RHEL
sudo apt install -y nfs-common    # Ubuntu

# Create mount point and mount
sudo mkdir -p /mnt/cow-storage
sudo mount -t nfs -o vers=3,nolock cow-host:/srv/cow-storage /mnt/cow-storage

# Persist in fstab
echo 'cow-host:/srv/cow-storage  /mnt/cow-storage  nfs  vers=3,nolock,_netdev  0  0' | sudo tee -a /etc/fstab
```

Replace `cow-host` with the hostname or IP of the daemon host.

**Verify:**

```bash
df -h /mnt/cow-storage
ls /mnt/cow-storage/
```

### Bind Mount on the Daemon Host (When CIDX Runs There Too)

If one of the CIDX cluster nodes runs on the same machine as the CoW daemon, that node cannot NFS-mount from itself. Instead, create a bind mount so the storage directory is available at the same `/mnt/cow-storage` path:

```bash
# Create mount point
sudo mkdir -p /mnt/cow-storage

# Bind mount
sudo mount --bind /srv/cow-storage /mnt/cow-storage

# Persist in fstab
echo '/srv/cow-storage  /mnt/cow-storage  none  bind  0  0' | sudo tee -a /etc/fstab
```

This ensures all nodes -- whether remote NFS clients or the local daemon host -- see clone contents at `/mnt/cow-storage`.

Note: `df -T /mnt/cow-storage` will show the underlying filesystem type (`xfs`) on the daemon host, not `nfs`. This is expected for a bind mount -- it exposes the same filesystem, not a network mount.

---

## Step 3: Configure CIDX Server

On each CIDX cluster node, update `~/.cidx-server/config.json` to use the cow-daemon clone backend.

### Required config.json Changes

Add the `clone_backend` and `cow_daemon` fields alongside your existing cluster configuration:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "log_level": "INFO",
  "storage_mode": "postgres",
  "postgres_dsn": "postgresql://cidx:password@db-host:5432/cidx_server",
  "cluster": {
    "node_id": "node-1"
  },
  "clone_backend": "cow-daemon",
  "cow_daemon": {
    "daemon_url": "http://cow-host:8081",
    "api_key": "your-cow-daemon-api-key",
    "mount_point": "/mnt/cow-storage",
    "poll_interval_seconds": 2,
    "timeout_seconds": 600
  }
}
```

### CowDaemonConfig Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `daemon_url` | Yes | `""` | Base URL of the CoW daemon REST API (e.g., `http://cow-host:8081`). |
| `api_key` | Yes | `""` | Bearer token matching the daemon's `api_key` config. |
| `mount_point` | Yes | `""` | Local NFS mount path where clone contents are visible (e.g., `/mnt/cow-storage`). |
| `poll_interval_seconds` | No | `2` | Initial poll interval (seconds) when waiting for async clone creation. Exponential backoff up to 30s. |
| `timeout_seconds` | No | `600` | Maximum time (seconds) to wait for a clone creation job to complete before raising `TimeoutError`. |

### Clone Backend Field

| Value | Description |
|-------|-------------|
| `"local"` | Default. Filesystem CoW via `cp --reflink=auto`. No external dependencies. |
| `"cow-daemon"` | REST client for the CoW Storage Daemon. Requires `cow_daemon` config. |
| `"ontap"` | ONTAP FlexClone volumes. Requires `ontap` config (production/FSx environments). |

### Apply on All Nodes

The `clone_backend` and `cow_daemon` settings must be identical on all cluster nodes (same `daemon_url`, same `api_key`, same `mount_point`). Only `cluster.node_id` differs per node.

After editing config on each node:

```bash
sudo systemctl restart cidx-server
```

---

## Step 4: Verify Startup

On each CIDX node, check the server logs for successful clone backend initialization:

```bash
journalctl -u cidx-server -f
```

Expected log messages:

```
INFO Building VersionedSnapshotManager with clone_backend='cow-daemon'
INFO Checking CoW daemon health at http://cow-host:8081/api/v1/health
INFO CoW daemon health check: OK
INFO Validating NFS mount at /mnt/cow-storage
INFO NFS mount validation: OK (latency=1.2ms)
INFO VersionedSnapshotManager: using CowDaemonBackend (daemon=http://cow-host:8081, mount=/mnt/cow-storage)
```

### Fail-Fast Behavior

CIDX enforces a fail-fast policy for the cow-daemon backend. At startup, two checks must pass:

1. **Daemon health check**: `GET /api/v1/health` must return HTTP 200 within 10 seconds.
2. **NFS mount validation**: The mount point must be accessible and writable (a probe file is written and read back).

If either check fails, the server raises `RuntimeError` and refuses to start. There is no fallback to a different backend. Fix the underlying issue (daemon not running, NFS not mounted) and restart.

---

## Step 5: Test Clone Lifecycle

Verify end-to-end clone creation by adding a golden repository through the CIDX admin interface or REST API. The server will use the CoW daemon to create versioned snapshots.

Monitor the daemon logs:

```bash
sudo journalctl -u cow-storage-daemon -f
```

Verify clones appear on the shared mount:

```bash
ls /mnt/cow-storage/
```

---

## cow-cli Reference

The `cow-cli` command-line tool wraps all daemon API endpoints. It is installed alongside the daemon.

### Connection Management

```bash
# Register a daemon
cow-cli connect prod http://cow-host:8081 --token <api-key>

# List connections (* = active)
cow-cli connections

# Switch active daemon
cow-cli activate prod
```

Connections are stored in `~/.cow-storage/config.json` (chmod 600, atomic writes).

### Clone Operations

```bash
# Create a clone (waits for completion by default)
cow-cli clone /path/to/source --namespace cidx --name my-clone

# Fire-and-forget (returns job ID immediately)
cow-cli clone /path/to/source --namespace cidx --name my-clone --nowait

# Check async job status
cow-cli job <job-id>

# List all clones
cow-cli list

# Filter by namespace
cow-cli list --namespace cidx

# Inspect a specific clone
cow-cli info cidx my-clone

# Delete a clone
cow-cli delete cidx my-clone --force
```

### Health and Stats

```bash
# Health check (active daemon)
cow-cli health

# Health check all registered daemons
cow-cli health --all

# Storage statistics
cow-cli stats
```

All commands support `--json` for scripting: `cow-cli --json list`.

---

## REST API Quick Reference

All endpoints are prefixed with `/api/v1`. Authentication via `Authorization: Bearer <api_key>`.

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/health` | Optional | Health check (unauthenticated when `health_requires_auth=false`). |
| `GET` | `/stats` | Yes | Storage statistics (disk usage, clone counts by namespace). |
| `POST` | `/clones` | Yes | Submit async clone creation job. Returns 202 with `job_id`. |
| `GET` | `/jobs/{job_id}` | Yes | Poll job status (`pending`, `running`, `completed`, `failed`). |
| `GET` | `/clones` | Yes | List all clones. Optional `?namespace=X` filter. |
| `GET` | `/clones/{ns}/{name}` | Yes | Get info for a specific clone. |
| `DELETE` | `/clones/{ns}/{name}` | Yes | Delete a clone (removes from disk and metadata). |

### Clone Creation Flow

1. `POST /clones` with `{"source_path": "...", "namespace": "...", "name": "..."}` returns 202 with `job_id`.
2. Poll `GET /jobs/{job_id}` until `status` is `completed` or `failed`.
3. On `completed`, `clone_path` contains the relative path (e.g., `cidx/my-clone`).
4. The CIDX `CowDaemonBackend` prepends `mount_point` to get the absolute filesystem path.

---

## Troubleshooting

### Daemon Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Daemon fails to start | Reflink not supported | Verify: `cp --reflink=always` test. Format with `mkfs.xfs -m reflink=1`. |
| Health check fails | Daemon not running | `sudo systemctl start cow-storage-daemon` |
| Clone creation fails | Source path not allowed | Add directory to `allowed_source_roots` in daemon config. |
| Clone creation fails | Source path does not exist | Verify golden repo is cloned at the expected path on the daemon host. |
| Job stuck in `pending` | Daemon overloaded | Check daemon logs: `sudo journalctl -u cow-storage-daemon -n 50`. |

### NFS Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `mount` hangs | Firewall blocking port 2049 | Open NFS ports on daemon host. |
| `access denied` | Wrong subnet in `/etc/exports` | Fix exports, run `sudo exportfs -ra`. |
| Clones not visible | NFS attribute cache | Run `ls` on the directory, or mount with `actimeo=1`. |
| `Permission denied` | UID/GID mismatch | Use `no_root_squash` in exports or match UIDs across nodes. |
| `Stale file handle` | Handle invalidated after restart | `sudo umount -f /mnt/cow-storage && sudo mount -a`. |
| `df` shows `xfs` not `nfs` | Bind mount (daemon host) | Expected for bind mounts -- the underlying filesystem type is shown. |

### CIDX Server Startup Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `RuntimeError: CoW daemon not reachable` | Daemon down or wrong URL | Start daemon, verify `daemon_url` in config.json. |
| `RuntimeError: NFS mount is not healthy` | Mount not present or not writable | Run `mount -a`, check `df -h /mnt/cow-storage`. |
| `cow_daemon_config is required` | Missing `cow_daemon` section | Add `cow_daemon` object to config.json. |
| `TimeoutError: CoW daemon job ... did not complete` | Clone took longer than `timeout_seconds` | Increase `timeout_seconds` or investigate daemon performance. |

### Verifying Clone Backend at Runtime

Check the server logs for the backend type at startup:

```bash
journalctl -u cidx-server | grep "VersionedSnapshotManager"
```

Expected output for cow-daemon:

```
INFO VersionedSnapshotManager: using CowDaemonBackend (daemon=http://cow-host:8081, mount=/mnt/cow-storage)
```

### Systemd Service Management

```bash
# Daemon
sudo systemctl {start|stop|restart|status} cow-storage-daemon
sudo journalctl -u cow-storage-daemon -f

# CIDX Server
sudo systemctl {start|stop|restart|status} cidx-server
sudo journalctl -u cidx-server -f
```

---

## Appendix: Storage Layout

The daemon organizes clones under `base_path` by namespace:

```
/srv/cow-storage/                   # base_path (daemon config)
  .cow-daemon.db                    # SQLite metadata (jobs, clones)
  cidx/                             # Namespace (used by CIDX Server)
    cidx_clone_my-repo_1700000000/  # Clone directory (reflink copy)
    cidx_clone_other-repo_170000/   # Another clone
  claude/                           # Another namespace
    claude_clone_abc_170000/        # Clone used by Claude Server
```

All cluster nodes see this layout at `/mnt/cow-storage/` via NFS (or bind mount on the daemon host).

---

## Appendix: Full config.json Example

A complete CIDX Server `config.json` for a cluster node using CoW daemon storage:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "log_level": "INFO",
  "storage_mode": "postgres",
  "postgres_dsn": "postgresql://cidx:password@db-host:5432/cidx_server",
  "server_dir": "~/.cidx-server",
  "cluster": {
    "node_id": "node-1"
  },
  "clone_backend": "cow-daemon",
  "cow_daemon": {
    "daemon_url": "http://cow-host:8081",
    "api_key": "your-cow-daemon-api-key",
    "mount_point": "/mnt/cow-storage",
    "poll_interval_seconds": 2,
    "timeout_seconds": 600
  }
}
```

Fields not shown retain their defaults. See [Configuration Guide](configuration.md) for all available settings.
