# CIDX Server Cluster Setup and Operations Guide

CIDX Clusterization

This guide covers installing and operating a CIDX Server cluster: fresh setup from scratch, converting an existing standalone server to cluster mode, adding nodes, HAProxy configuration, and troubleshooting. For the architecture explanation, see [Cluster Architecture Guide](cluster-architecture.md).

## Prerequisites

### PostgreSQL

- PostgreSQL 15 or later (recommended; the schema uses `TIMESTAMPTZ`, `JSONB`, `SERIAL`, and session-level advisory locks that are available in all recent versions)
- A dedicated database and user for CIDX Server
- Network connectivity from all cluster nodes to the PostgreSQL host on the database port (default 5432)

### Python Dependencies

Cluster mode requires psycopg v3 and psycopg-pool, which are not installed by the base `pip install -e .`. They are declared as the `cluster` optional dependency group in `pyproject.toml`:

```toml
[project.optional-dependencies]
cluster = [
    "psycopg[binary]>=3.1.0",
    "psycopg-pool>=3.1.0",
]
```

Install with:

```bash
pip install -e ".[cluster]"
```

Or install the packages directly (as the install script does):

```bash
python3 -m pip install "psycopg[binary]" psycopg-pool requests numpy
```

The binary distribution of psycopg includes the C extension and libpq. It is sufficient for both the connection pool and the advisory lock connection used by `LeaderElectionService`.

### Load Balancer

HAProxy or any HTTP load balancer that distributes requests across the node IP addresses on port 8090. Session affinity (sticky sessions) is recommended: the Research Assistant feature runs Claude CLI in a background thread on the node that received the request, and poll responses are only available from that same node until the result is persisted to the database. Use HAProxy cookie-based affinity (`cookie SERVERID insert indirect nocache`) to pin a browser session to one node.

### Shared Storage

Golden repositories (source code clones) must be accessible at the same filesystem path on each node. Two approaches are supported:

- **NFS** (simple): Export a directory from one node, mount on all others. The install script installs `nfs-utils` (RHEL/Rocky) or `nfs-common` (Ubuntu/Debian) as a system dependency.
- **CoW Storage Daemon** (recommended for non-production): Provides FlexClone-equivalent functionality using filesystem reflinks, with a REST API for clone lifecycle management and NFS for file access. See [CoW Storage Setup Guide](cow-storage-setup.md) for full instructions.

**CRITICAL -- NFS mount options**: All NFS mounts MUST use `soft,timeo=30,retrans=3` (NOT `hard`). The `hard` option causes server threads to enter uninterruptible D-state (`nfs_wait_bit_killable`) on any NFS blip, permanently blocking dep-map analysis and other background jobs. The `soft` option returns EIO after timeout instead of blocking forever. Example fstab entry:

```
192.168.60.23:/path/to/export /mnt/cow-storage nfs4 soft,timeo=30,retrans=3,_netdev 0 0
```

**Exception -- the golden-repos mount**: When using the CoW Storage Daemon, the golden-repos directory itself is a separate NFS mount that MUST use `vers=3,nolock,hard` (NFSv3, not NFSv4, and `hard` not `soft`). Git's `index-pack` step during golden-repo indexing mmaps its pack and index files and fails on an NFSv4 mount and on a `soft` mount. This is a deliberate difference from the clone mount above; apply `hard` specifically to the golden-repos mount. See [Golden Repository Shared Storage (CoW Daemon Dev Clusters)](#golden-repository-shared-storage-cow-daemon-dev-clusters) below for the full configuration and rationale.

---

## Fresh Cluster Setup from Scratch

This section assumes you are setting up a new cluster with no existing CIDX Server data.

Note: The `scripts/` directory contains automation scripts that perform many of these steps:
- `scripts/cluster-join.sh` -- Automates joining a new node to an existing cluster
- `scripts/cluster-migrate.sh` -- Automates SQLite-to-PostgreSQL data migration
- `scripts/cluster-join-test.sh` -- Test harness for cluster join
- `scripts/cluster-migrate-lib.sh` -- Shared library used by the scripts above

These scripts can be used instead of the manual steps below.

### Step 1: Install and Configure PostgreSQL

On the PostgreSQL host:

```bash
# Install PostgreSQL (example for Rocky Linux 9 / RHEL 9)
sudo dnf install -y postgresql-server postgresql
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql

# Create database and user
sudo -u postgres psql <<EOF
CREATE USER cidx WITH PASSWORD 'your-strong-password';
CREATE DATABASE cidx_server OWNER cidx;
GRANT ALL PRIVILEGES ON DATABASE cidx_server TO cidx;
EOF
```

Verify connectivity from a cluster node:

```bash
psql "postgresql://cidx:your-strong-password@db-host:5432/cidx_server" -c "SELECT version();"
```

### Step 2: Install CIDX Server on the First Node

Run the install script on the first node. The script is idempotent and handles Rocky Linux, RHEL, and Ubuntu/Debian:

```bash
bash scripts/install-cidx-server.sh \
  --branch master \
  --voyage-key your-voyage-api-key \
  --port 8090
```

What the script does:
1. Installs system packages: `git`, `nfs-utils` (or `nfs-common`), `gcc`, `python3-pip`, `python3-devel`
2. Clones the repository to `~/code-indexer` (or pulls if already present)
3. Runs `python3 -m pip install -e .` followed by `pip install "psycopg[binary]" psycopg-pool requests numpy`
4. Creates `~/.cidx-server/data/golden-repos/`, `~/.cidx-server/logs/`, `~/.cidx-server/locks/`
5. Creates a default `~/.cidx-server/config.json` with `storage_mode: "sqlite"` if none exists
6. Creates, enables, and starts the `cidx-server` systemd service
7. Installs and enables the `cidx-auto-update.service` and `cidx-auto-update.timer` units, tracking `--branch` (default `master`) -- see [Auto-Update Service](#auto-update-service)

Verify the installation completes without errors:

```bash
systemctl status cidx-server
```

### Step 3: Configure config.json for Cluster Mode

Stop the server before editing config:

```bash
sudo systemctl stop cidx-server
```

Edit `~/.cidx-server/config.json` to set PostgreSQL mode and the node identity:

```json
{
  "host": "0.0.0.0",
  "port": 8090,
  "log_level": "INFO",
  "storage_mode": "postgres",
  "postgres_dsn": "postgresql://cidx:your-strong-password@db-host:5432/cidx_server",
  "cluster": {
    "node_id": "node-1"
  }
}
```

Configuration fields:
- `storage_mode`: must be `"postgres"` to enable cluster services
- `postgres_dsn`: libpq connection string; the same DSN is used by the connection pool and the leader election advisory lock connection
- `cluster.node_id`: unique identifier for this node; appears in log messages, `cluster_nodes` table, and background job `executing_node` field

Warning: always set `cluster.node_id` explicitly. If the field is absent or empty, different services apply different fallback formats. Cluster services (leader election, heartbeat, job reconciliation) fall back to `{hostname}-cidx` using `os.uname().nodename`. `NodeMetricsWriterService` falls back to `socket.gethostname()` with no suffix. When these differ, heartbeat records and metrics records for the same physical node carry different node identifiers, making cross-service correlation unreliable.

### Step 4: Run PostgreSQL Migrations

The migration runner applies all numbered `.sql` files in `src/code_indexer/server/storage/postgres/migrations/sql/` in numeric order. It is idempotent: already-applied migrations are skipped.

Note: Migrations are also run automatically on server startup (Story #519). This manual step is optional but useful for validating the PostgreSQL connection before starting the server for the first time.

```bash
cd ~/code-indexer
PYTHONPATH=src python3 -m code_indexer.server.storage.postgres.migrations.runner \
  --connection-string "postgresql://cidx:your-strong-password@db-host:5432/cidx_server"
```

Expected output (example — the count increases with new versions; there are 29 migrations (001-029)):
```
INFO Applied migration: 001_initial_schema.sql
INFO Applied migration: 002_groups_access_schema.sql
INFO Applied migration: 003_node_metrics.sql
...
Applied 29 migration(s).
```

Check migration status at any time:

```bash
PYTHONPATH=src python3 -m code_indexer.server.storage.postgres.migrations.runner \
  --connection-string "postgresql://cidx:your-strong-password@db-host:5432/cidx_server" \
  --status
```

### Step 5: Start the Server

```bash
sudo systemctl start cidx-server
journalctl -u cidx-server -f
```

Confirm cluster mode in the logs:

```
INFO Storage mode: PostgreSQL (cluster)
INFO Cluster services started: node_id=node-1, is_leader=True
INFO NodeHeartbeatService [node-1]: started (interval=10s)
INFO JobReconciliationService: started (sweep_interval=5s, max_execution_time=1800s)
INFO LeaderElectionService [node-1]: monitor started (interval=10s)
INFO LeaderElectionService [node-1]: acquired leadership (lock_id=0x434944585f4c4452)
INFO NodeMetricsWriterService started (node_id=node-1)
```

The first node to start will acquire leadership (the pg_advisory_lock). It runs scheduler services (golden repo refresh, etc.) in addition to handling requests.

Verify the `cluster_nodes` table:

```bash
psql "postgresql://cidx:your-strong-password@db-host:5432/cidx_server" \
  -c "SELECT node_id, hostname, status, role, last_heartbeat FROM cluster_nodes;"
```

### Step 6: Configure HAProxy

Add an HAProxy configuration for the CIDX Server backend. Each server line points to one cluster node:

```
backend cidx_servers
    balance roundrobin
    option httpchk GET /healthz
    cookie SERVERID insert indirect nocache
    server node-1 192.168.1.11:8090 check cookie node1
    server node-2 192.168.1.12:8090 check cookie node2

frontend cidx_frontend
    bind *:80
    default_backend cidx_servers
```

The `cookie SERVERID insert indirect nocache` directive enables session affinity. Each node is assigned a cookie value (`node1`, `node2`). Once a client receives the cookie, subsequent requests are routed to the same node. This is required for the Research Assistant feature and recommended for consistent Web UI behavior.

The health check targets `/healthz` -- a dedicated, unauthenticated liveness/readiness endpoint, separate from the detailed `/api/system/health` diagnostics endpoint. `/healthz` maps the computed overall status directly to the HTTP status code: HTTP 200 for `healthy` or `degraded` (degraded is still serviceable and must not be drained), HTTP 503 for `unhealthy` (for example, when the golden-repos storage read-probe added for Bug #1433 detects a broken NFS/CoW mount). Because HAProxy's default `httpchk` behavior already treats any 2xx/3xx as up and everything else as down, a plain `option httpchk GET /healthz` is sufficient -- no `http-check expect` body-parsing directive is needed. `/healthz`'s response body is intentionally minimal (`{"status": "..."}` only, no service/system detail) since it is reachable without authentication.

Note for institutional memory: an earlier version of this doc pointed the check at `option httpchk GET /api/system/health` with `http-check expect ! string "\"status\":\"unhealthy\""`. That combination does not work: `/api/system/health` requires authentication, so an unauthenticated HAProxy probe receives HTTP 401 on every check and never sees the JSON body the `http-check expect` directive was trying to match against -- the storage-failure signal never reached HAProxy's up/down decision. Do not reintroduce a body-matching check against an authenticated endpoint; use the dedicated `/healthz` endpoint instead. A plain `GET /docs` check only proves the FastAPI process is alive, not that the node can actually serve queries -- `/healthz` is required for that signal.

After adding all nodes, reload HAProxy:

```bash
sudo systemctl reload haproxy
```

---

## Converting a Standalone (SQLite) Server to Cluster Mode

Use this procedure when you have an existing single-node CIDX Server with data in SQLite that you want to migrate to cluster mode.

### Step 1: Install Cluster Dependencies

On the existing server:

```bash
cd ~/code-indexer
python3 -m pip install "psycopg[binary]" psycopg-pool requests numpy
```

### Step 2: Set Up PostgreSQL

Follow Step 1 from the fresh setup section above. Create the PostgreSQL database and user, verify connectivity.

### Step 3: Run PostgreSQL Migrations

```bash
cd ~/code-indexer
PYTHONPATH=src python3 -m code_indexer.server.storage.postgres.migrations.runner \
  --connection-string "postgresql://cidx:your-strong-password@db-host:5432/cidx_server"
```

### Step 4: Migrate SQLite Data to PostgreSQL

The migration tool reads from both SQLite databases and writes to PostgreSQL. It uses `INSERT ... ON CONFLICT DO NOTHING` throughout, so it is safe to re-run if interrupted.

Stop the server first to prevent new writes during migration:

```bash
sudo systemctl stop cidx-server
```

Run the migration tool:

```bash
cd ~/code-indexer
PYTHONPATH=src python3 -m code_indexer.server.tools.migrate_to_postgres \
  --sqlite-path ~/.cidx-server/data/cidx_server.db \
  --groups-path ~/.cidx-server/groups.db \
  --pg-url "postgresql://cidx:your-strong-password@db-host:5432/cidx_server"
```

The tool migrates tables from `cidx_server.db` in dependency order:

```
users, user_api_keys, user_mcp_credentials, user_oidc_identities,
invalidated_sessions, password_change_timestamps, repo_categories,
global_repos, golden_repos_metadata, background_jobs, sync_jobs,
ci_tokens, ssh_keys, ssh_key_hosts, description_refresh_tracking,
dependency_map_tracking, self_monitoring_scans, self_monitoring_issues,
research_sessions, research_messages, diagnostic_results,
wiki_cache, wiki_sidebar_cache, user_git_credentials
```

And from `groups.db`:

```
groups, user_group_membership, repo_group_access, audit_logs
```

JSON text columns are parsed and inserted as JSONB. Boolean 0/1 integers are converted to PostgreSQL booleans.

### Step 5: Update config.json and Restart

Edit `~/.cidx-server/config.json` to add the cluster fields (see Step 3 of the fresh setup). Then:

```bash
sudo systemctl start cidx-server
journalctl -u cidx-server -f
```

Verify the cluster mode log messages appear as described in Step 5 of the fresh setup.

---

## Adding Nodes to an Existing Cluster

Each additional node follows this procedure. The PostgreSQL database and migrations are already in place.

### Step 1: Install CIDX Server on the New Node

```bash
bash scripts/install-cidx-server.sh \
  --branch master \
  --voyage-key your-voyage-api-key \
  --port 8090
```

Stop the server after installation to configure it before it starts:

```bash
sudo systemctl stop cidx-server
```

The installer also provisions the `cidx-auto-update` units on this node; set `--branch` to the branch this cluster tracks (for example `staging`) so the new node does not silently deploy a different branch than its peers -- see [Auto-Update Service](#auto-update-service).

### Step 2: Configure config.json

Edit `~/.cidx-server/config.json` with the same `postgres_dsn` as the other nodes, but a unique `node_id`:

```json
{
  "host": "0.0.0.0",
  "port": 8090,
  "log_level": "INFO",
  "storage_mode": "postgres",
  "postgres_dsn": "postgresql://cidx:your-strong-password@db-host:5432/cidx_server",
  "cluster": {
    "node_id": "node-2"
  }
}
```

Each node must have a distinct `node_id`. Duplicate node IDs cause heartbeat conflicts in the `cluster_nodes` table (the table has a PRIMARY KEY on `node_id`).

### Step 2b: JWT Secret Sharing

All cluster nodes must share the same JWT signing secret so that authentication tokens issued by one node are accepted by all others.

In PostgreSQL mode, JWT secret sharing is automatic. The `JWTSecretManager` stores the shared secret in the `cluster_secrets` PostgreSQL table. The first node to start generates the secret and writes it to PostgreSQL; subsequent nodes read it from the database. No manual file copying is required.

Note: The file `~/.cidx-server/.jwt_secret` may still exist on each node as a local cache, but the authoritative source in cluster mode is the `cluster_secrets` table in PostgreSQL.

### Step 2c: Open Firewall Port

The CIDX server listens on port 8090. If the OS firewall is active (firewalld on Rocky Linux / RHEL), open the port:

```bash
sudo firewall-cmd --add-port=8090/tcp --permanent
sudo firewall-cmd --reload
```

Without this, the load balancer health checks will fail and the node will not receive traffic.

### Step 3: Mount Shared Storage

Mount the NFS volume containing golden repositories at the same path as the other nodes, for example `/home/cidx/.cidx-server/data/golden-repos`. The exact mount path depends on your infrastructure; it must match the `target_path` and `clone_path` values stored in the PostgreSQL `global_repos` and `golden_repos_metadata` tables.

For a CoW Storage Daemon dev cluster, the golden-repos directory is a separate NFSv3 mount with specific options (`vers=3,nolock,hard`) and the daemon host uses a bind mount instead. Configure it per [Golden Repository Shared Storage (CoW Daemon Dev Clusters)](#golden-repository-shared-storage-cow-daemon-dev-clusters) -- do not skip the mount options, or golden repos will not be cross-node visible.

### Step 4: Start the Server

```bash
sudo systemctl start cidx-server
journalctl -u cidx-server -f
```

The new node will:
1. Start all cluster services (heartbeat, metrics writer, job reconciliation)
2. Attempt leader election; since the existing node holds the lock, this node becomes a worker
3. Register itself in `cluster_nodes` with `role='worker'`
4. Begin accepting HTTP requests immediately

Verify in PostgreSQL:

```bash
psql "postgresql://cidx:your-strong-password@db-host:5432/cidx_server" \
  -c "SELECT node_id, hostname, status, role, last_heartbeat FROM cluster_nodes ORDER BY registered_at;"
```

### Step 5: Add to HAProxy Backend

Add a new `server` line to the HAProxy backend configuration and reload:

```
server node-2 192.168.1.12:8090 check
```

```bash
sudo systemctl reload haproxy
```

---

## Dashboard Monitoring

The admin web UI includes a cluster health carousel showing per-node metrics. The data comes from `node_metrics` snapshots written by `NodeMetricsWriterService` every 5 seconds, read via `NodeMetricsBackend.get_latest_per_node()`.

The dashboard displays per node:
- CPU usage percent
- Memory usage (percent and used bytes)
- Process RSS (CIDX Server process memory)
- Swap usage
- Disk I/O rates (KB/s read and write)
- Network I/O rates (KB/s receive and transmit)
- Volume usage (per mount point: total, used, free GB and percent)
- Server version
- Node IP address

The carousel shows one card per distinct `node_id` in the latest snapshots. In a three-node cluster, three cards rotate through.

The node role (`scheduler` vs `worker`) is shown on the heartbeat panel, populated from the `cluster_nodes.role` column. The node running the scheduler (the leader) shows `scheduler`; all others show `worker`.

---

## Golden Repository Shared Storage (CoW Daemon Dev Clusters)

When a development cluster uses the CoW Storage Daemon (see [CoW Storage Setup Guide](cow-storage-setup.md)) as its shared backend, golden repositories require handling that is easy to get wrong. The configuration below was verified live while debugging a three-node cluster. Follow it exactly.

### Golden Repos Must Live on the Shared Mount

The server derives the golden-repos directory as `<server_dir>/data/golden-repos`: `golden_repos_dir = Path(server_data_dir) / "data" / "golden-repos"` (`src/code_indexer/server/startup/lifespan.py:134`). This subpath is hard-coded. It is NOT independently configurable -- there is no config field to relocate it. On startup the server also runs an NFS atomic-create self-check against the `cidx-meta` subdirectory of this path (`run_nfs_atomic_create_self_check(Path(golden_repos_dir) / "cidx-meta")`, `lifespan.py:1386`), so the path must be a functioning shared filesystem.

Therefore, on EVERY cluster node, `~/.cidx-server/data/golden-repos` MUST resolve to the shared cow-storage storage, never per-node local disk.

- Daemon host (the node that runs cow-storage-daemon): bind-mount a `golden-repos` subdirectory of the daemon `base_path` onto the golden-repos path. A node cannot NFS-mount from itself, so it uses a bind mount.

```bash
sudo mkdir -p ~/.cidx-server/data/golden-repos
sudo mount --bind /srv/cow-storage/golden-repos ~/.cidx-server/data/golden-repos
# Persist in fstab
echo '/srv/cow-storage/golden-repos  /home/<user>/.cidx-server/data/golden-repos  none  bind  0  0' | sudo tee -a /etc/fstab
```

- Every other node: NFS-mount that same daemon-side `golden-repos` subdirectory at the same path (mount options below).

Failure mode if this stays on local disk: golden repos are only queryable on the node that indexed them. A query that HAProxy routes to any other node returns 0 results, because that node's local `golden-repos` directory does not contain the index. The repos are not cross-node visible.

### NFS Mount Options (NFSv3, nolock, hard)

Mount the golden-repos subdirectory with NFSv3 and client-side locking -- NOT NFSv4:

```bash
sudo mount -t nfs -o vers=3,nolock,hard,_netdev \
  <daemon-host>:/srv/cow-storage/golden-repos ~/.cidx-server/data/golden-repos
# Persist in fstab
echo '<daemon-host>:/srv/cow-storage/golden-repos  /home/<user>/.cidx-server/data/golden-repos  nfs  vers=3,nolock,hard,_netdev  0  0' | sudo tee -a /etc/fstab
```

Why NFSv3 with `nolock`, not NFSv4: mounting golden-repos over NFSv4 (`nfs4` / `vers=4.x`) causes `git index-pack` to fail during golden-repo clone+index with `fatal: write error: Bad file descriptor` followed by `fatal: fetch-pack: invalid index-pack output`. This is NFSv4 locking plus mmap semantics interacting badly with git's pack writing. NFSv3 with `nolock` (client-side locking, equivalent to `local_lock=all`) avoids it. Reproduced and fixed live.

Why `hard`, not `soft`: a `soft` mount returns an I/O error on a transient timeout, which surfaces as SIGBUS on the mmap'd pack/index files git touches during indexing. `hard` blocks and retries instead, which is what git's mmap path requires here. This is a deliberate exception to the general clone-mount guidance in the [Shared Storage](#shared-storage) prerequisite (which recommends `soft` for `/mnt/cow-storage` to prevent D-state hangs in background jobs). Apply `hard` specifically to the golden-repos mount.

### NFS Export Must Be async (Dev)

On the daemon host, export the cow-storage directory with `async`, not `sync`, for dev clusters:

```
/home/<user>/cow-storage  <subnet>/24(rw,async,no_subtree_check,no_root_squash)
```

Why: `sync` forces a server-side commit on every write, making golden-repo indexing pathologically slow (measured ~65x slower). `async` acknowledges writes before they reach stable storage. This is acceptable for dev-only installs, where the single cow-storage host is already a documented single point of failure (see below). Do not use `async` where write durability matters.

### Topology and Single Point of Failure

In this dev model the cow-storage-daemon runs on ONE node and acts as the shared disk -- an ONTAP-like emulator for development. CoW clone operations go through the daemon's REST API; the resulting storage is consumed by the other nodes over NFS. Both golden repos and cow-daemon clones live on this single shared disk. If the cow-storage host goes down, the cluster loses its shared storage and is effectively down. This single point of failure is by design and acceptable for development installs only.

### Known Limitation: Temporal Indexing over NFS

Per-commit dual-embedder TEMPORAL indexing (Epic #1289) over the NFS golden-repos mount is latency-bound: it performs many small HNSW quarterly-shard writes, and over NFS this is currently slow enough that a temporal-index job can exceed the background `JobReconciliationService` `max_execution_time` and be reaped/failed.

Regular SEMANTIC golden-repo indexing over NFS works correctly and is cross-node-queryable. Temporal-on-NFS performance is an open issue. Until it is optimized, run temporal indexing on local storage or raise the job timeout.

---

## Auto-Update Service

Each node keeps itself current by pulling and redeploying the tracked git branch on a timer. Understanding the topology matters because the application server does NOT update itself -- a separate systemd unit does, and if that unit is missing the node silently never advances.

### Two-Service Topology (WHY it is split)

There are two distinct systemd units per node, with separate responsibilities:

- `cidx-server.service` -- the long-running application server. It only SIGNALS that it wants to be restarted (it writes a restart-signal file and advances a `launch_restart_generation` target). It never pulls, builds, or restarts itself.
- `cidx-auto-update.service` -- a `Type=oneshot` unit that runs as the install user with `PrivateTmp=yes`. It is what actually fetches, installs, and restarts the application. It is fired by `cidx-auto-update.timer` every 60 seconds (`OnUnitActiveSec=60`).

The split exists so that an in-place code update can restart the server cleanly from an outside process, rather than a process trying to replace its own running image.

Failure mode if the auto-update units are absent (for example on a node installed before the units were provisioned): the node never self-updates, and `cidx-server` logs a line like the following indefinitely, because its requested restart generation is never applied:

```
launch_restart_generation target > applied ... check cidx-auto-update service status
```

That log line is the primary symptom of a node that is missing (or has a stopped) auto-update timer.

### What Each Fire Does

`cidx-auto-update.service` runs `python3 -m code_indexer.server.auto_update.run_once`. It reads the branch to track from the `CIDX_AUTO_UPDATE_BRANCH` environment variable set in the service unit; if that variable is unset it defaults to `master`.

On each fire it performs:

1. `git fetch` of the tracked branch. If the remote tip equals the local checkout, it does nothing and exits.
2. If the remote tip differs, it runs a deployment: `git pull`, then build/install (hnswlib submodule, `pip install`, ripgrep, Rust toolchain if missing, pace-maker, Claude CLI), then systemd/config hardening (`MALLOC_ARENA_MAX`, the sudoers self-restart rule, memory overcommit, swap), then restart `cidx-server`.
3. If the update changed the updater's OWN code, the service self-restarts mid-deploy: it writes a pending-redeploy marker, re-executes, and resumes so the deploy always finishes running the new code.

The FIRST deploy on a fresh node is heavy -- it compiles hnswlib and installs the Rust toolchain -- and can take several minutes. Subsequent deploys (no toolchain build) are fast.

### How the Installer Provisions It (v11.21.0+)

`scripts/install-cidx-server.sh` installs and enables BOTH units. It renders `cidx-auto-update.service` from the shipped template at `src/code_indexer/server/auto_update/templates/cidx-auto-update.service`, substituting the install user, the repository path, and the branch. The same template backs both the installer and the CLI retrofit command below, so the unit text is never duplicated.

Two flags control the branch:

- `--branch` -- the git branch to install and, by default, the branch the auto-updater tracks.
- `--auto-update-branch` -- overrides the auto-update branch independently. When omitted it defaults to whatever `--branch` is.

CRITICAL: a staging node MUST be installed with the branch set to `staging`, otherwise the auto-updater tracks `master` and the node will silently deploy production code. Set it explicitly:

```bash
bash scripts/install-cidx-server.sh \
  --branch staging \
  --voyage-key <voyage-api-key> \
  --port 8090
```

To track a different branch than the one checked out (rare), add `--auto-update-branch <branch>`.

### Retrofitting an Existing Node

A node installed before the installer provisioned these units has `cidx-server.service` but no `cidx-auto-update` units, and shows the `launch_restart_generation target > applied` symptom above. To retrofit it, run the shipped CLI command on that node (available as of v11.21.0):

```bash
cidx server install-auto-update --branch staging
```

This renders the service template (substituting the install user, repository path, and branch), installs both `cidx-auto-update.service` and `cidx-auto-update.timer` into `/etc/systemd/system/`, runs `systemctl daemon-reload`, and enables and starts the timer. Use `--branch master` for a production node.

Before retrofitting, confirm the repository checkout is already ON the branch you intend to track -- the auto-updater pulls that branch into the existing checkout.

### Operate and Troubleshoot

Check that the timer is active and see when it next fires:

```bash
systemctl status cidx-auto-update.timer
systemctl list-timers cidx-auto-update.timer
```

Watch deploys as they happen:

```bash
journalctl -u cidx-auto-update -f
```

Verify which branch a node is tracking (look for `CIDX_AUTO_UPDATE_BRANCH`):

```bash
systemctl show cidx-auto-update.service -p Environment
```

Confirm the version actually applied after a deploy:

```bash
cidx --version
```

Expected log lines that are NOT errors:

- `Failed with result 'signal'` during a deploy that changed the updater itself. This is the self-restart seam (the service re-execs to finish the deploy on the new code), not a failure.
- `Failed to enter maintenance mode: 401`. This benign warning appears when no admin token is available; the deploy proceeds anyway.

### Robustness Note (v11.21.0)

A heavy first deploy (compiling hnswlib, installing Rust) is CPU-bound and transiently starves systemd and sudo, because `pam_systemd` blocks while PID 1 is busy. This is worsened if a `hard` NFS mount points at an unreachable node, since operations on that mount hang. To keep the systemd control-plane steps (daemon-reload, `systemctl restart`, the sudoers edits) from being silently skipped under that pressure, the deployment executor uses a 120-second per-attempt timeout with bounded retry on those operations (`SYSTEMD_OP_TIMEOUT_SECONDS`, `_run_systemd_op_with_retry` in `deployment_executor.py`).

This interacts with the single-point-of-failure note in [Topology and Single Point of Failure](#topology-and-single-point-of-failure) above: if the CoW/NFS host node is down, `hard` NFS mounts on the other nodes hang, which can stall systemd operations there and delay or fail an in-progress deploy on every node. Restore the CoW/NFS host before expecting auto-updates to complete cluster-wide.

---

## Troubleshooting

### Verifying Leader Election State

Check which node holds the advisory lock. The lock ID is `0x434944585f4c4452`:

```sql
SELECT pid, application_name, query_start, state
FROM pg_stat_activity
WHERE state = 'idle'
AND EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory'
    AND classid = (x'43494458' :: int)
    AND objid = (x'5f4c4452' :: int)
    AND pid = pg_stat_activity.pid
);
```

Alternatively, check the `cluster_nodes` table for the node with `role='scheduler'`:

```sql
SELECT node_id, hostname, role, last_heartbeat FROM cluster_nodes WHERE status = 'online';
```

### Verifying Heartbeat Activity

If a node's `last_heartbeat` is more than 30 seconds old (the default `active_threshold_seconds`), that node will not appear in `get_active_nodes()` results. Jobs it was running will be reclaimed by `JobReconciliationService` on the next sweep.

```sql
SELECT node_id, status, role,
       last_heartbeat,
       NOW() - last_heartbeat AS age
FROM cluster_nodes
ORDER BY last_heartbeat DESC;
```

A healthy running node updates its heartbeat every 10 seconds. An age over 30 seconds indicates the heartbeat thread has stopped or the node is unreachable.

### Leader Failover Verification

To test failover: stop CIDX Server on the leader node and observe a follower node acquire leadership.

On the leader node:
```bash
sudo systemctl stop cidx-server
```

Within approximately 10 seconds (one monitor thread interval), the PostgreSQL advisory lock is released (the dedicated connection closes when the process stops). The next monitor thread wakeup on a follower calls `pg_try_advisory_lock` and succeeds.

On a follower node, the log shows:
```
INFO LeaderElectionService [node-2]: acquired leadership (lock_id=0x434944585f4c4452)
INFO Cluster services started: node_id=node-2, is_leader=True
```

The `cluster_nodes` table updates to show `node-2` with `role='scheduler'`.

### PostgreSQL Connection Loss Recovery

If a node loses PostgreSQL connectivity:
- The heartbeat thread logs an exception on each tick but continues retrying.
- The leader election monitor detects the dedicated connection is dead (the `SELECT 1` ping fails), relinquishes leadership, and retries `pg_try_advisory_lock` on the next iteration.
- Jobs continue processing if they were already in memory; no new jobs can be claimed because the job backend is unavailable.

When connectivity is restored, the services recover automatically on the next iteration of their respective background threads (10 seconds for leader election and heartbeat, 5 seconds for job reconciliation).

### Stale Node Detection

A node that crashes without a graceful shutdown leaves its row in `cluster_nodes` with `status='online'` but `last_heartbeat` stops updating. `JobReconciliationService.sweep()` calls `get_active_nodes()` which excludes nodes with stale heartbeats (older than `active_threshold_seconds`, default 30 seconds). Any jobs claimed by the crashed node are reset to `pending` on the next sweep.

To manually clean up a stale node entry:

```sql
UPDATE cluster_nodes SET status = 'offline' WHERE node_id = 'crashed-node';
```

Or delete it entirely:

```sql
DELETE FROM cluster_nodes WHERE node_id = 'crashed-node';
```

### Checking background_jobs for Orphaned Jobs

Running jobs that were abandoned by a crashed node will have `status='running'` and an `executing_node` that no longer appears as active. To find them:

```sql
SELECT job_id, operation_type, executing_node, started_at, status
FROM background_jobs
WHERE status = 'running'
  AND executing_node NOT IN (
      SELECT node_id FROM cluster_nodes
      WHERE status = 'online'
        AND last_heartbeat >= NOW() - INTERVAL '30 seconds'
  );
```

`JobReconciliationService` resets these automatically within 5 seconds of the next sweep.

### Common Errors

**"postgres_dsn required when storage_mode=postgres"**

`config.json` has `storage_mode: "postgres"` but no `postgres_dsn` field. Add the `postgres_dsn` key with a valid connection string.

**"FATAL: PostgreSQL configured but initialization failed: ... Refusing to start"**

The server could not connect to PostgreSQL on startup and has refused to start (Bug #532 fail-fast behavior). The server process exits with a RuntimeError. The log message contains the underlying psycopg exception. Check:
- PostgreSQL is running and reachable at the DSN host and port
- The database user has `CONNECT` privilege on the database
- Network firewalls allow the connection
- The `postgres_dsn` in config.json is correct (host, port, database name, credentials)
- Migrations are applied automatically on startup, but if migration itself fails, the server also refuses to start

**"psycopg (v3) is not installed; leader election is not available"**

The `psycopg` package is not installed. Run:

```bash
python3 -m pip install "psycopg[binary]" psycopg-pool requests numpy
```

**Duplicate node_id in cluster_nodes**

If two nodes have the same `node_id` in `config.json`, they will share a heartbeat row. One node's heartbeat will overwrite the other's hostname. Jobs claimed by either node will have the same `executing_node` value, which confuses job reconciliation. Set distinct `node_id` values in each node's `config.json`.

**Migration checksum error**

If a `.sql` migration file is modified after being applied, the `MigrationRunner` will detect the checksum mismatch and refuse to apply subsequent migrations. Do not modify migration files after they are applied to any database. If you need to change the schema, create a new numbered migration file.
