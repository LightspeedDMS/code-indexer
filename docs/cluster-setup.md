# CIDX Server Cluster Setup and Operations Guide

Epic #408: CIDX Clusterization

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

HAProxy or any HTTP load balancer that distributes requests across the node IP addresses on port 8000. The nodes do not need sticky sessions; all state is in the shared PostgreSQL database.

### Shared Storage

Golden repositories (source code clones) must be accessible at the same filesystem path on each node. A common approach is NFS. The install script installs `nfs-utils` (RHEL/Rocky) or `nfs-common` (Ubuntu/Debian) as a system dependency.

---

## Fresh Cluster Setup from Scratch

This section assumes you are setting up a new cluster with no existing CIDX Server data.

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
  --branch epic/408-cidx-clusterization \
  --voyage-key your-voyage-api-key \
  --port 8000
```

What the script does:
1. Installs system packages: `git`, `nfs-utils` (or `nfs-common`), `gcc`, `python3-pip`, `python3-devel`
2. Clones the repository to `~/code-indexer` (or pulls if already present)
3. Runs `python3 -m pip install -e .` followed by `pip install "psycopg[binary]" psycopg-pool requests numpy`
4. Creates `~/.cidx-server/data/golden-repos/`, `~/.cidx-server/logs/`, `~/.cidx-server/locks/`
5. Creates a default `~/.cidx-server/config.json` with `storage_mode: "sqlite"` if none exists
6. Creates, enables, and starts the `cidx-server` systemd service

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
  "port": 8000,
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

```bash
cd ~/code-indexer
PYTHONPATH=src python3 -m code_indexer.server.storage.postgres.migrations.runner \
  --connection-string "postgresql://cidx:your-strong-password@db-host:5432/cidx_server"
```

Expected output:
```
INFO Applied migration: 001_initial_schema.sql
INFO Applied migration: 002_groups_access_schema.sql
INFO Applied migration: 003_node_metrics.sql
Applied 3 migration(s).
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
    option httpchk GET /health
    server node-1 192.168.1.11:8000 check
    server node-2 192.168.1.12:8000 check

frontend cidx_frontend
    bind *:80
    default_backend cidx_servers
```

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
  --branch epic/408-cidx-clusterization \
  --voyage-key your-voyage-api-key \
  --port 8000
```

Stop the server after installation to configure it before it starts:

```bash
sudo systemctl stop cidx-server
```

### Step 2: Configure config.json

Edit `~/.cidx-server/config.json` with the same `postgres_dsn` as the other nodes, but a unique `node_id`:

```json
{
  "host": "0.0.0.0",
  "port": 8000,
  "log_level": "INFO",
  "storage_mode": "postgres",
  "postgres_dsn": "postgresql://cidx:your-strong-password@db-host:5432/cidx_server",
  "cluster": {
    "node_id": "node-2"
  }
}
```

Each node must have a distinct `node_id`. Duplicate node IDs cause heartbeat conflicts in the `cluster_nodes` table (the table has a PRIMARY KEY on `node_id`).

### Step 3: Mount Shared Storage

Mount the NFS volume containing golden repositories at the same path as the other nodes, for example `/home/cidx/.cidx-server/data/golden-repos`. The exact mount path depends on your infrastructure; it must match the `index_path` and `clone_path` values stored in the PostgreSQL `global_repos` and `golden_repos_metadata` tables.

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
server node-2 192.168.1.12:8000 check
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

**"Failed to initialize PostgreSQL backends: ... Falling back to SQLite"**

The server could not connect to PostgreSQL on startup. The error message in the log contains the underlying psycopg exception. Check:
- PostgreSQL is running and reachable at the DSN host and port
- The database user has `CONNECT` privilege on the database
- Network firewalls allow the connection
- The migrations have been applied (missing tables cause immediate errors on first backend use)

**"psycopg (v3) is not installed; leader election is not available"**

The `psycopg` package is not installed. Run:

```bash
python3 -m pip install "psycopg[binary]" psycopg-pool requests numpy
```

**Duplicate node_id in cluster_nodes**

If two nodes have the same `node_id` in `config.json`, they will share a heartbeat row. One node's heartbeat will overwrite the other's hostname. Jobs claimed by either node will have the same `executing_node` value, which confuses job reconciliation. Set distinct `node_id` values in each node's `config.json`.

**Migration checksum error**

If a `.sql` migration file is modified after being applied, the `MigrationRunner` will detect the checksum mismatch and refuse to apply subsequent migrations. Do not modify migration files after they are applied to any database. If you need to change the schema, create a new numbered migration file.
