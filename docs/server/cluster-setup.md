# Cluster Setup

Operator procedure for running CIDX Server as a cluster: several nodes sharing one PostgreSQL database and one
shared storage tree, behind a load balancer. For how the cluster works (leader election, heartbeats, job claiming,
shared state), see [Cluster Architecture](../architecture/cluster.md). For a single node, see
[Deployment](deployment.md).

## Prerequisites

- **PostgreSQL** reachable from every node, with a dedicated database and user. Each node holds a connection pool
  plus one dedicated connection for the leader-election advisory lock.
- **Shared storage** that every node mounts at the same path. The installer and auto-updater provision the CoW
  Storage Daemon layout (`clone_backend: cow-daemon`): one NFSv3 mount per node with `golden-repos` and
  `activated-repos` symlinked into it. Set it up first with [CoW Storage Setup](cow-storage-setup.md).
- **A load balancer** (HAProxy in the examples) in front of the nodes' HTTP port (default 8000).
- **A unique node id per node** (`cluster.node_id`).

An ONTAP FlexClone backend (`clone_backend: ontap`, `ontap` bootstrap section) also exists, provisioned by
`scripts/cluster-join.sh` and `scripts/cluster-migrate.sh`. This guide covers the CoW daemon procedure.

## Step 1: Create the database

On the PostgreSQL host:

```bash
sudo -u postgres psql <<'EOF'
CREATE USER cidx WITH PASSWORD '<strong password>';
CREATE DATABASE cidx_server OWNER cidx;
EOF
```

From a future cluster node:

```bash
psql "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server" -c "SELECT version();"
```

## Step 2: Install the first node

Cluster mode is enabled when the installer receives both `--node-id` and `--postgres-dsn`. Preview with
`--dry-run`, then run:

```bash
bash scripts/install-cidx-server.sh \
  --branch master \
  --node-id node-1 \
  --postgres-dsn "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server" \
  --clone-backend cow-daemon \
  --cow-daemon-url "http://192.0.2.20:8081" \
  --cow-daemon-api-key <daemon api key> \
  --nfs-server 192.0.2.20 \
  --nfs-export /srv/cow-storage \
  --cow-daemon-storage-path /srv/cow-storage \
  --workers 2
```

`--cow-daemon-storage-path` is the daemon's own `base_path` on the daemon host (here the same directory as the
export). It becomes `cow_daemon.daemon_storage_path`, which every clone request needs to translate paths under
`/mnt/cow-storage` into the daemon's local paths. Pass it on every node, including the daemon host. Without it
every CoW clone (per-user activation and versioned-snapshot publish) fails with `CoW daemon_storage_path is not
configured`. The installer and the auto-updater can detect the value only by reading
`/etc/cow-storage-daemon/config.json`, which exists only on the daemon host and which the daemon installer writes
mode 600 for the daemon's own user; it is detected only when the cidx-server user can read that file. The other
fallback is `CIDX_COW_DAEMON_STORAGE_PATH` in the installer's or the `cidx-auto-update` unit's environment.

In addition to the standalone steps listed in [Deployment](deployment.md#what-the-installer-does-standalone),
cluster mode:

1. Mounts the CoW storage at `/mnt/cow-storage` and verifies it is writable.
2. Tests the PostgreSQL connection and runs the schema migrations
   (`python3 -m code_indexer.server.storage.postgres.migrations.runner --connection-string <dsn>`).
3. On the daemon host only, makes the daemon user a member of its `service_group`.
4. Creates the `golden-repos` and `activated-repos` symlinks into the mount.
5. Merges `storage_mode: postgres`, `postgres_dsn`, `cluster.node_id`, `clone_backend` and `cow_daemon` into
   `config.json` (backing up the previous file, mode 600).
6. Opens the port in firewalld when firewalld is active.
7. After the server starts, prints the `cluster_nodes` rows.

The server also applies pending migrations on every start; a failed migration stops startup. The DSN is passed to
the migration runner on its command line, so it is visible in the process list while the runner executes.

## Step 3: Verify the node

```bash
journalctl -u cidx-server -f
```

Expected lines include `Storage mode: PostgreSQL (cluster)`, `NodeHeartbeatService [node-1]: started
(interval=10s)`, `LeaderElectionService [node-1]: acquired leadership (lock_id=...)` on the first node, and
`Cluster services started: node_id=node-1, ...`.

```bash
psql "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server" \
  -c "SELECT node_id, hostname, status, role, last_heartbeat FROM cluster_nodes ORDER BY registered_at;"
```

The leader shows `role = scheduler`; every other node shows `worker`.

## Step 4: Add nodes

Run the same installer command on each additional node with its own `--node-id`. On the CoW daemon host, if it
also runs CIDX, add `--cow-local-bind` (see [CoW Storage Setup](cow-storage-setup.md#bind-mount-on-the-daemon-host)).

To re-run the installer on an existing node, pass the same full set of flags it was installed with; only
`--node-id` may be dropped (it is then reused from `config.json`). Flags you leave out fall back to their
defaults and overwrite the node's settings: without `--clone-backend cow-daemon` the merged `config.json` gets
`clone_backend: local`, and the `cidx-server` unit is rewritten with `--port 8000` and `--workers 1` unless
`--port` and `--workers` are given again.

Node identity rules:

- Every node needs a distinct `cluster.node_id`. Two nodes with the same id share one `cluster_nodes` row (its
  primary key) and the same `executing_node` value on jobs, which breaks dead-node job reclaim.
- Always set it explicitly. When it is empty, services fall back to different hostname-derived identifiers, so a
  node's heartbeat and metrics rows stop matching.

Nodes share the JWT signing secret through the `cluster_secrets` table: the first node to start generates it, the
others read it, so a token issued by one node is accepted by all.

## Step 5: Load balancer

```
backend cidx_servers
    balance roundrobin
    option httpchk GET /healthz
    server node-1 192.0.2.11:8000 check
    server node-2 192.0.2.12:8000 check

frontend cidx_frontend
    bind *:80
    default_backend cidx_servers
```

`/healthz` is unauthenticated and returns HTTP 200 for `healthy` and `degraded` and HTTP 503 for `unhealthy`, so a
plain `httpchk` drains exactly the unhealthy nodes. `/health` and `/api/system/health` require authentication and
are not usable as an unauthenticated probe. What makes a node `unhealthy` (for example an unreadable golden-repos
directory, or RAM at 90 percent or more) is listed in [Observability](observability.md).

Session affinity is not required. Cross-request state (search payloads, job status, Research Assistant job
polling) is resolved through the shared database, so a request can land on any node.

Do not route `/api/admin/maintenance/enter` or `/api/admin/maintenance/exit` through the load balancer; they are
for each node's local auto-updater (see [Maintenance and Jobs](maintenance-and-jobs.md)).

## Golden Repository Shared Storage

Every node must resolve `~/.cidx-server/data/golden-repos` to the same shared directory; otherwise a repository is
queryable only on the node that indexed it. The server derives that path from the data directory and has no
setting to move it. With the CoW daemon backend it is a symlink to `/mnt/cow-storage/golden-repos`, created by the
installer and repaired by the auto-updater. The full layout, mount options and the reasons there is no separate
golden-repos mount are in [CoW Storage Setup](cow-storage-setup.md#storage-layout).

## Converting a standalone node to cluster mode

1. Create the database (Step 1).
2. Stop the server: `sudo systemctl stop cidx-server`.
3. Apply the schema:

   ```bash
   cd ~/code-indexer
   PYTHONPATH=src python3 -m code_indexer.server.storage.postgres.migrations.runner \
     --connection-string "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server"
   ```

4. Copy the SQLite data into PostgreSQL with the offline migration tool. Run it only while every server using that
   database is stopped:

   ```bash
   PYTHONPATH=src python3 -m code_indexer.server.tools.migrate_to_postgres \
     --sqlite-path ~/.cidx-server/data/cidx_server.db \
     --groups-path ~/.cidx-server/groups.db \
     --oauth-path ~/.cidx-server/oauth.db \
     --refresh-tokens-path ~/.cidx-server/refresh_tokens.db \
     --scip-audit-path ~/.cidx-server/scip_audit.db \
     --server-dir ~/.cidx-server \
     --pg-url "postgresql://cidx:<password>@192.0.2.10:5432/cidx_server"
   ```

   `--validate-only` compares row counts without migrating; `--table` migrates one table; `--nfs-mount` also moves
   `~/.claude` and `~/.cidx-server/research` onto the shared mount behind symlinks. Run
   `python3 -m code_indexer.server.tools.migrate_to_postgres --help` for every option.

5. Re-run the installer with the cluster flags (Step 2). If `~/.cidx-server/data/golden-repos` is a non-empty
   directory, the installer moves it to `~/.cidx-server/data/golden-repos.legacy.bug1337` and points the symlink at
   `/mnt/cow-storage/golden-repos`. It does not copy the repositories. Either copy the contents of the
   `.legacy.bug1337` directory into `/mnt/cow-storage/golden-repos` before starting, or re-register the
   repositories after the start. Neither step is automated.

## Auto-update in a cluster

Each node runs its own `cidx-auto-update` timer and deploys independently (see [Auto-Update](auto-update.md)).
Install every node of one environment with the same branch. A Web UI restart in cluster mode advances a shared
restart generation; every node's auto-updater then applies the launch settings and restarts its own server.

Schema migrations are additive (new tables, columns and indexes only) so nodes on adjacent versions can share the
database during a rolling update. Migrations take a PostgreSQL advisory lock, so concurrent starts apply them once.

## Jobs across nodes

- A node runs the jobs it claimed; the claiming node is recorded in `background_jobs.executing_node`.
- The leader runs `JobReconciliationService` every 5 seconds. A `running` job whose `executing_node` has no
  heartbeat in the last 30 seconds goes back to `pending` for another node to claim. A running job on a live node
  is never reclaimed, however long it runs: indexing jobs have no wall-clock timeout.
- The only time-based reclaim is for a malformed row: `running` with no `started_at`, older than
  `max_execution_time` (the startup log prints `max_execution_time=1800s`). Such a row is set to `failed`.
- Cancelling a job owned by another node writes the cancellation to the database; the owning node sees it on its
  next cancellation check. See [Maintenance and Jobs](maintenance-and-jobs.md).

## Troubleshooting

### Which node is the leader

```sql
SELECT node_id, hostname, role, last_heartbeat FROM cluster_nodes WHERE status = 'online';
```

The leader holds PostgreSQL advisory lock `0x434944585f4c4452`. When the leader stops, its lock connection closes
and a follower acquires the lock on its next 10-second check.

### Stale nodes and orphaned jobs

A node that crashed keeps `status = 'online'` but its `last_heartbeat` stops advancing; after 30 seconds it is no
longer active and its running jobs are reclaimed on the next sweep.

```sql
SELECT node_id, status, role, last_heartbeat, NOW() - last_heartbeat AS age
FROM cluster_nodes ORDER BY last_heartbeat DESC;

SELECT job_id, operation_type, executing_node, started_at
FROM background_jobs
WHERE status = 'running'
  AND executing_node NOT IN (
      SELECT node_id FROM cluster_nodes
      WHERE status = 'online' AND last_heartbeat >= NOW() - INTERVAL '30 seconds');
```

Remove a decommissioned node's row with `DELETE FROM cluster_nodes WHERE node_id = '<node id>';`.

### PostgreSQL connection loss

Heartbeat and leader-election threads keep retrying; the leader drops leadership when its lock connection fails.
Both recover on their next iteration once the database is reachable.

### Startup errors

| Message | Cause |
|---------|-------|
| `postgres_dsn required when storage_mode=postgres` | `config.json` has `storage_mode: postgres` without `postgres_dsn` |
| `FATAL: PostgreSQL configured but initialization failed: ...` | Database unreachable, wrong credentials, or no `CONNECT` privilege; the server refuses to start |
| `FATAL: PostgreSQL schema migration failed: ...` | A migration failed; the server refuses to start |
| `RuntimeError: CoW daemon not reachable ...` / `NFS mount is not healthy ...` | See [CoW Storage Setup](cow-storage-setup.md#troubleshooting) |
