# CIDX Server Cluster Architecture

Epic #408: CIDX Clusterization

This document describes the architecture of CIDX Server when running in cluster mode (multiple nodes sharing a PostgreSQL database). For setup and operational procedures, see [Cluster Setup Guide](cluster-setup.md).

## Overview

CIDX Server supports two storage modes, selected by the `storage_mode` field in `~/.cidx-server/config.json`:

- `sqlite` (default): Standalone single-node server. All data stored in local SQLite files under `~/.cidx-server/data/`. No cluster services are started.
- `postgres`: Cluster mode. All data stored in a shared PostgreSQL database. Cluster services (leader election, heartbeat, job reconciliation) start automatically on server startup.

The storage mode switch is the only required configuration change to move from standalone to cluster operation. All application logic, REST endpoints, and MCP tools are identical in both modes because all storage access goes through Protocol interfaces (see Storage Abstraction below).

## Component Overview

A typical cluster deployment consists of:

- Two or more CIDX Server nodes, each running the same application code
- A shared PostgreSQL database (version 15 or later recommended)
- A load balancer (such as HAProxy) distributing HTTP requests across nodes
- Shared storage for the golden repository files (NFS or equivalent), mounted at the same path on each node

Each node runs independently and handles requests without inter-node communication. Coordination happens exclusively through the shared PostgreSQL database.

### Cluster Topology Diagram

```
                        Clients (Browser, MCP, REST)
                                   |
                                   v
                       +-----------------------+
                       |     Load Balancer     |
                       |      (HAProxy)        |
                       |   roundrobin :8000    |
                       +-----+-----+----------+
                             |     |
                +------------+     +------------+
                |                               |
                v                               v
     +-------------------+           +-------------------+
     |   CIDX Node 1     |           |   CIDX Node 2     |
     |   (192.168.68.9)  |           |  (192.168.68.21)  |
     |                   |           |                   |
     |  uvicorn :8000    |           |  uvicorn :8000    |
     |  Leader Election  |           |  Leader Election  |
     |  Heartbeat        |           |  Heartbeat        |
     |  Metrics Writer   |           |  Metrics Writer   |
     |  Job Execution    |           |  Job Execution    |
     |  Query Serving    |           |  Query Serving    |
     +--------+----------+           +--------+----------+
              |                               |
              +---------------+---------------+
                              |
               +--------------+---------------+
               |                              |
               v                              v
    +--------------------+       +------------------------+
    |    PostgreSQL       |       |    Shared Storage      |
    |  (192.168.68.43)   |       |    (NFS / ONTAP)       |
    |                    |       |                        |
    |  - users           |       |  golden-repos/         |
    |  - sessions        |       |  .versioned/           |
    |  - global_repos    |       |  .code-indexer/index/  |
    |  - background_jobs |       |                        |
    |  - cluster_nodes   |       |                        |
    |  - node_metrics    |       |                        |
    |  - groups          |       |                        |
    |  - audit_logs      |       |                        |
    +--------------------+       +------------------------+
```

### Request Flow

```
    Client Request
         |
         v
    HAProxy (:8000)
         |  roundrobin with health check (GET /docs, 5s interval)
         |
    +----+----+
    |         |
    v         v
  Node 1    Node 2      <-- Both can serve ANY request
    |         |
    |    +----+----+
    |    |         |
    v    v         v
  PostgreSQL    Shared FS    <-- Shared state + shared files
```

### Leader-Only vs All-Node Services

```
  +-------------------------------------------+
  |              ALL NODES                     |
  |                                           |
  |  - HTTP/REST/MCP request handling         |
  |  - Query serving (semantic, FTS, SCIP)    |
  |  - Job execution (claim from shared queue)|
  |  - NodeHeartbeatService (10s interval)    |
  |  - NodeMetricsWriterService (5s interval) |
  |  - HNSW/FTS/Payload cache (per-node)      |
  |  - Web UI (session-pinned via HAProxy)    |
  +-------------------------------------------+

  +-------------------------------------------+
  |           LEADER NODE ONLY                 |
  |     (one node holds pg_advisory_lock)      |
  |                                           |
  |  - RefreshScheduler (golden repo pulls)   |
  |  - DataRetentionScheduler (cleanup)       |
  |  - DescriptionRefreshScheduler            |
  |  - DependencyMapScheduler                 |
  |  - CidxMetaRefreshDebouncer              |
  |  - JobReconciliationService (5s sweep)    |
  +-------------------------------------------+

  Leader failover: if the leader node dies, its PostgreSQL
  connection drops, releasing the advisory lock. Another node
  acquires the lock within 10 seconds (monitor check interval)
  and starts the leader-only services.
```

## Storage Abstraction

### Protocol Interfaces

All storage operations in CIDX Server go through Python Protocol interfaces defined in `src/code_indexer/server/storage/protocols.py`. Each backend area (users, sessions, repositories, background jobs, SSH keys, etc.) has a dedicated Protocol:

- `GlobalReposBackend` - repository registry
- `UsersBackend` - user and API key management
- `SessionsBackend` - session invalidation
- `BackgroundJobsBackend` - background job tracking
- `SyncJobsBackend` - sync job tracking
- `CITokensBackend` - CI/CD token storage
- `SSHKeysBackend` - SSH key management
- `GoldenRepoMetadataBackend` - golden repository metadata
- `DependencyMapTrackingBackend` - dependency map state
- `DescriptionRefreshTrackingBackend` - description refresh tracking
- `GitCredentialsBackend` - per-user git credentials
- `RepoCategoryBackend` - repository categories
- `GroupsBackend` - group access management
- `AuditLogBackend` - audit log writes and queries
- `NodeMetricsBackend` - per-node system metrics snapshots
- `LogsBackend` - server log storage
- `ApiMetricsBackend` - API call metrics
- `PayloadCacheBackend` - search result payload cache
- `OAuthBackend` - OAuth token and client management
- `SCIPAuditBackend` - SCIP code intelligence audit records
- `RefreshTokenBackend` - refresh token storage
- `ResearchSessionsBackend` - research assistant sessions
- `WikiCacheBackend` - wiki article view counts
- `SelfMonitoringBackend` - self-monitoring diagnostic results
- `DiagnosticsBackend` - system diagnostics results
- `MaintenanceBackend` - maintenance mode coordination

All Protocols are decorated with `@runtime_checkable`, enabling `isinstance()` checks in tests and at runtime. Application code depends only on these Protocol interfaces, never on concrete backend classes.

### Backend Registry

`StorageFactory.create_backends()` in `src/code_indexer/server/storage/factory.py` reads `storage_mode` from the server config dict and returns a `BackendRegistry` dataclass containing one concrete backend instance per Protocol. The application receives a `BackendRegistry` and accesses all storage through it.

### Factory Pattern

The factory has two private paths:

`_create_sqlite_backends(data_dir)`: Instantiates all SQLite backend classes from `src/code_indexer/server/storage/sqlite_backends.py`. The main database is `~/.cidx-server/data/cidx_server.db`. Groups and audit log use `~/.cidx-server/groups.db`.

`_create_postgres_backends(config)`: Instantiates all PostgreSQL backend classes from `src/code_indexer/server/storage/postgres/`. All PostgreSQL backends share a single `ConnectionPool` instance wrapping `psycopg_pool.ConnectionPool` with default `min_size=1`, `max_size=20`. The pool size is configurable via the `postgres_pool_max_size` field in `config.json`. psycopg imports are lazy inside this method; standalone servers never import psycopg.

When `storage_mode` is an unrecognized value, `StorageFactory.create_backends()` raises `ValueError`.

### Mode Detection at Startup

If `storage_mode` is `"postgres"` but PostgreSQL initialization fails (connection refused, authentication error, migration failure), the server logs a FATAL error and **refuses to start**. This fail-fast behavior (Bug #532) prevents a cluster node from silently operating on local SQLite, which would cause data divergence across the cluster within minutes. The server process exits with a `RuntimeError` containing the underlying connection error. To recover, fix the PostgreSQL connection or change `storage_mode` to `"sqlite"` in `config.json`.

## PostgreSQL Connection Pool

`src/code_indexer/server/storage/postgres/connection_pool.py` wraps `psycopg_pool.ConnectionPool` with a simplified context-manager interface:

```python
with pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
```

Default pool sizes: `min_size=1`, `max_size=20` (configurable via `postgres_pool_max_size` in config.json). The pool is initialized with `open=True`, establishing connections immediately on construction. Callers must not close the connection; the pool reclaims it on context exit.

## Cluster Services

When `storage_mode` is `"postgres"`, the application lifespan handler in `src/code_indexer/server/startup/lifespan.py` starts three additional background services after the `BackendRegistry` is created. These services run on every node.

### Leader Election Service

`src/code_indexer/server/services/leader_election_service.py`

Implements cluster-wide leader election using PostgreSQL's `pg_try_advisory_lock`. Exactly one node in the cluster holds the advisory lock at any time. The node holding the lock is the leader and runs scheduler services (golden repository refresh, dependency map, description refresh, etc.).

The lock identifier is the 64-bit integer `0x434944585F4C4452` (ASCII encoding of "CIDX_LDR"). This constant is identical on every node.

The lock is held on a dedicated psycopg connection that is kept open for the duration of leadership. This is the key mechanism: PostgreSQL automatically releases an advisory lock when the connection that holds it closes, whether by graceful shutdown or by network failure or process crash. Another node can then acquire the lock.

The dedicated lock connection is configured with TCP keepalive parameters: `keepalives=1`, `keepalives_idle=10`, `keepalives_interval=5`, `keepalives_count=3`. This detects dead connections at the TCP level within approximately 25 seconds, complementing the application-level `SELECT 1` ping. Without TCP keepalive, a half-open TCP connection (caused by network partition or firewall state loss) could leave a ghost leader for the duration of the OS TCP timeout (typically 15+ minutes on Linux).

Leadership acquisition uses `SELECT pg_try_advisory_lock(%s)` with `autocommit=True`. A session-level advisory lock (as opposed to a transaction-level lock) remains held until the connection closes, not until the transaction ends.

The service runs a background monitor thread (`LeaderElection-{node_id}`) that wakes every 10 seconds (configurable via `check_interval` parameter). On each iteration:

1. If this node is currently the leader, the monitor pings the dedicated connection with `SELECT 1`. If the connection is dead, leadership is relinquished locally, `on_lose_leadership()` completes, and re-election is deferred to the next monitor loop iteration (preventing a race between stopping and starting scheduler services).
2. If this node is not the leader, `try_acquire_leadership()` is called to attempt acquisition.

Leadership transition callbacks are registered at startup:
- `on_become_leader`: called when this node acquires the lock. Starts scheduler services.
- `on_lose_leadership`: called when this node loses the lock. Stops scheduler services.

The leader election service is stored in `app.state.leader_election` so other parts of the application can check `app.state.leader_election.is_leader`.

On graceful shutdown, `stop_monitoring()` calls `release_leadership()`, which closes the dedicated connection and triggers PostgreSQL to release the advisory lock. The next node to attempt `pg_try_advisory_lock` will succeed.

### Node Heartbeat Service

`src/code_indexer/server/services/node_heartbeat_service.py`

Maintains a heartbeat record for this node in the `cluster_nodes` PostgreSQL table. Other services (notably `JobReconciliationService`) use this table to determine which nodes are alive when reclaiming abandoned jobs.

The `cluster_nodes` table schema (defined in `001_initial_schema.sql`):

```sql
CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id         TEXT        PRIMARY KEY,
    hostname        TEXT        NOT NULL,
    port            INTEGER     NOT NULL DEFAULT 8000,
    status          TEXT        NOT NULL DEFAULT 'active',
    role            TEXT        NOT NULL DEFAULT 'worker',
    last_heartbeat  TIMESTAMPTZ,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB
);
```

On `start()`, the service:
1. Creates `cluster_nodes` if absent (idempotent DDL).
2. Upserts this node's row with `status='online'` and the current hostname from `os.uname().nodename`.
3. Starts a daemon thread (`NodeHeartbeat-{node_id}`) that runs every 10 seconds (configurable via `heartbeat_interval` parameter, default 10).

On each heartbeat tick, `update_heartbeat()` sets `last_heartbeat=NOW()` and sets `role` to `'scheduler'` if the leader election service reports `is_leader=True`, otherwise `'worker'`. This allows the dashboard to display which node is currently the scheduler leader.

`get_active_nodes()` returns node IDs where `status='online'` and `last_heartbeat >= NOW() - active_threshold_seconds`. Default `active_threshold_seconds` is 30. This method is called by `JobReconciliationService` on every sweep.

On `stop()`, the service marks the node as `status='offline'` via upsert before the thread exits.

### Job Reconciliation Service

`src/code_indexer/server/services/job_reconciliation_service.py`

Runs a background sweep every 5 seconds (configurable via `sweep_interval` parameter, default 5) to detect and reclaim background jobs abandoned by crashed or offline nodes.

Two reclaim conditions are evaluated on each sweep:

**Dead-node reclaim**: Queries `get_active_nodes()` from the heartbeat service. Any `background_jobs` row with `status='running'` and `executing_node` not in the active-nodes list is reset to `status='pending'` with `executing_node=NULL`, `started_at=NULL`, `claimed_at=NULL`. If the active-nodes list is empty (e.g., during a transient heartbeat outage), this reclaim is skipped to prevent false positives.

**Execution-timeout reclaim**: Any `background_jobs` row with `status='running'` and `started_at <= NOW() - max_execution_time` seconds is reset to pending. Default `max_execution_time` is 1800 seconds (30 minutes). This is a safety net for runaway jobs.

Reclaimed jobs are reset to `'pending'` so any healthy node can pick them up on the next job executor poll.

The service uses the same `ConnectionPool` as the heartbeat service. Both use the standard PostgreSQL connection pool, not the dedicated advisory lock connection.

### Node Metrics Writer Service

`src/code_indexer/server/services/node_metrics_writer_service.py`

This service runs on every node regardless of storage mode (SQLite or PostgreSQL). It collects system metrics every 5 seconds (configurable via `write_interval` parameter, default 5) using psutil and writes snapshots to the `NodeMetricsBackend`.

Metrics collected per snapshot:

- `node_id`: node identifier string
- `node_ip`: detected local non-loopback IP address (via UDP connect trick to 8.8.8.8:80)
- `timestamp`: UTC ISO-8601 timestamp
- `cpu_usage`: CPU percent (non-blocking, `psutil.cpu_percent(interval=None)`)
- `memory_percent`, `memory_used_bytes`: system memory usage
- `process_rss_mb`: RSS of the CIDX Server process in megabytes
- `index_memory_mb`: total memory used by in-memory HNSW and FTS index caches in megabytes (currently hardcoded to 0.0; reserved for future index memory tracking)
- `swap_used_mb`, `swap_total_mb`: swap usage
- `disk_read_kb_s`, `disk_write_kb_s`: disk I/O rate in KB/s (delta from previous snapshot)
- `net_rx_kb_s`, `net_tx_kb_s`: network I/O rate in KB/s (delta from previous snapshot)
- `volumes_json`: JSON array of mounted partition usage (mount point, device, fstype, total/used/free GB, percent)
- `server_version`: from `code_indexer.__version__`

I/O rates are computed as deltas between consecutive snapshots divided by elapsed time. On the first snapshot, all rates are 0.0 (no baseline yet). Negative deltas (counter reset or OS reboot) are clamped to 0.0.

Old snapshots are cleaned up automatically. Default retention is 3600 seconds (1 hour). The cleanup runs after each write cycle by calling `backend.cleanup_older_than(cutoff)`.

The node_id for the metrics writer is read from `config.json` at startup (`cluster.node_id`). If absent, `socket.gethostname()` is used (no `-cidx` suffix; see the note in the `cluster.node_id` configuration field description).

In cluster (PostgreSQL) mode, the writer uses `backend_registry.node_metrics` (a `NodeMetricsPostgresBackend`). In standalone mode, it creates a dedicated `NodeMetricsSqliteBackend` pointing to `~/.cidx-server/data/cidx_server.db`.

## Database Schema

### Migration System

PostgreSQL schema is managed by numbered SQL migration files in `src/code_indexer/server/storage/postgres/migrations/sql/`. The `MigrationRunner` class in `src/code_indexer/server/storage/postgres/migrations/runner.py` discovers `.sql` files by numeric prefix, applies pending ones in order, and records each in the `schema_migrations` table with an MD5 checksum.

Migrations are forward-only. Once applied, a migration file must not be modified (the checksum detects tampering).

In PostgreSQL mode, `MigrationRunner.run()` is called automatically during server startup (Story #519), before backends are created. If any migration fails, the server refuses to start. This ensures the database schema is always up to date after a code deployment. Manual migration invocation is still supported but no longer required for normal operation.

Current migrations:

- `001_initial_schema.sql`: All tables including users, sessions, global_repos, background_jobs, ssh_keys, cluster_nodes, node_metrics (this migration also includes the node_metrics table before migration 003 was added)
- `002_groups_access_schema.sql`: Replaces the groups tables from migration 001 with the full GroupAccessManager schema (groups, user_group_membership, repo_group_access, audit_logs with admin_id/action_type/target_type/target_id columns)
- `003_node_metrics.sql`: Creates the `node_metrics` table used by `NodeMetricsWriterService`

### node_metrics Table

```sql
CREATE TABLE IF NOT EXISTS node_metrics (
    id                  SERIAL          PRIMARY KEY,
    node_id             TEXT            NOT NULL,
    node_ip             TEXT            NOT NULL,
    timestamp           TIMESTAMPTZ     NOT NULL,
    cpu_usage           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    memory_percent      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    memory_used_bytes   BIGINT          NOT NULL DEFAULT 0,
    process_rss_mb      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    index_memory_mb     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    swap_used_mb        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    swap_total_mb       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    disk_read_kb_s      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    disk_write_kb_s     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    net_rx_kb_s         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    net_tx_kb_s         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    volumes_json        JSONB            NOT NULL DEFAULT '[]',
    server_version      TEXT            NOT NULL DEFAULT ''
);
```

Indexes: `(node_id, timestamp DESC)` for per-node latest query; `(timestamp)` for cleanup.

### cluster_nodes Table

```sql
CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id         TEXT        PRIMARY KEY,
    hostname        TEXT        NOT NULL,
    port            INTEGER     NOT NULL DEFAULT 8000,
    status          TEXT        NOT NULL DEFAULT 'active',
    role            TEXT        NOT NULL DEFAULT 'worker',
    last_heartbeat  TIMESTAMPTZ,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB
);
```

Index: `(status)` for active-node queries.

### background_jobs Table (cluster-relevant columns)

The `background_jobs` table has two cluster-specific columns added in migration 001:

- `executing_node TEXT`: identifies which cluster node claimed and is running this job
- `claimed_at TIMESTAMPTZ`: when the job was claimed by a node

These columns are `NULL` for jobs in `pending` or `completed`/`failed` states.

### Distributed Job Claiming

`src/code_indexer/server/services/distributed_job_claimer.py`

In cluster mode, jobs are claimed atomically using PostgreSQL's `FOR UPDATE SKIP LOCKED` pattern. When a node picks up a job from the shared queue, it executes:

```sql
UPDATE background_jobs SET executing_node = %s, status = 'running', claimed_at = NOW()
WHERE job_id = (SELECT job_id FROM background_jobs WHERE status = 'pending' FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *
```

This guarantees exactly-once claiming across concurrent nodes. The `SKIP LOCKED` clause ensures that if another node is already claiming a job, this node skips it and picks the next available one. All subsequent status updates (`complete_job`, `fail_job`, `release_job`) include an `AND executing_node = %s` guard to prevent one node from accidentally modifying another node's job.

## Configuration Fields

The following fields in `~/.cidx-server/config.json` control cluster behavior:

### storage_mode

Type: string. Default: `"sqlite"`.

Valid values: `"sqlite"` or `"postgres"`. Set to `"postgres"` to enable cluster mode. All cluster services (leader election, heartbeat, job reconciliation) start only when this is `"postgres"`.

### postgres_dsn

Type: string. Required when `storage_mode` is `"postgres"`.

PostgreSQL connection string in libpq format. Example: `"postgresql://cidx:password@db-host:5432/cidx_server"`. Used by the `ConnectionPool`, all PostgreSQL backends, and the `LeaderElectionService` (which opens a separate dedicated connection for the advisory lock).

### cluster.node_id

Type: string under the `cluster` object. Default: empty string.

Unique identifier for this node within the cluster. Used in log messages, heartbeat records, job claiming, and metrics. Each node in the cluster must have a distinct `node_id`.

Important: always set `cluster.node_id` explicitly. Different services use different fallback formats when this field is absent or empty:

- Cluster services (leader election, heartbeat, job reconciliation) in `lifespan.py` fall back to `{hostname}-cidx` using `os.uname().nodename`.
- `NodeMetricsWriterService` falls back to `socket.gethostname()` with no suffix.

This means the node_id recorded in `cluster_nodes` (heartbeat) and the node_id recorded in `node_metrics` can differ if the field is not set explicitly, which makes correlating metrics with cluster node state unreliable.

Example configuration:

```json
{
  "storage_mode": "postgres",
  "postgres_dsn": "postgresql://cidx:password@192.168.1.10:5432/cidx_server",
  "cluster": {
    "node_id": "node-1"
  }
}
```

### postgres_pool_max_size

Type: integer. Default: `20`.

Maximum number of connections in the PostgreSQL connection pool. All backends share this pool. Increase for high-concurrency deployments or when running many concurrent background jobs.

### ontap (OntapConfig)

Type: object under the top-level config. Optional.

`ServerConfig` includes an `OntapConfig` dataclass (defined in `src/code_indexer/server/utils/config_manager.py`) for ONTAP FlexClone shared storage configuration. This is intended for environments where golden repository snapshots are managed via NetApp ONTAP FlexClone volumes instead of standard NFS mounts. This is Phase 4 of Epic #408 and is not yet fully implemented. The field is parsed from config.json but the FlexClone provisioning workflow is not active in the current release.

## What Runs on Every Node vs. Leader Only

### Every Node

- NodeHeartbeatService: heartbeat updates every 10 seconds
- NodeMetricsWriterService: metrics snapshots every 5 seconds
- JobReconciliationService: orphaned job sweep every 5 seconds
- LeaderElectionService monitor thread: attempts to acquire leadership every 10 seconds
- All HTTP request handlers (REST API, MCP, Web UI)
- Background job executor: picks up pending jobs from the shared queue

### Leader Only (node holding the pg_advisory_lock)

- Golden repository refresh scheduler
- Description refresh scheduler
- Dependency map scheduler
- Catch-up processing scheduler
- Any other scheduler services registered via the `on_become_leader` callback

When the leader node goes offline, PostgreSQL releases the advisory lock automatically (connection drop). Within 10 seconds, another node's monitor thread calls `pg_try_advisory_lock` and succeeds. That node's `on_become_leader` callback fires and starts the scheduler services.

## Cache Invalidation

Each node maintains its own in-memory HNSW and FTS index caches. There is no cross-node cache invalidation in the current implementation. When one node modifies an index (for example, after a golden repository refresh on the leader), other nodes' caches become stale until they expire based on the configured TTL (`cache_config.index_cache_ttl_minutes`, default 10 minutes).

For most deployments this is acceptable because:
- Golden repository refreshes happen on a schedule (typically hourly)
- Stale cache entries expire and are reloaded from shared storage on next access
- The load balancer does not pin sessions to specific nodes

## SQLite-to-PostgreSQL Data Migration

When converting an existing standalone server to cluster mode, the migration tool at `src/code_indexer/server/tools/migrate_to_postgres.py` copies data from the SQLite databases to PostgreSQL.

The tool reads from two SQLite sources:
- `~/.cidx-server/data/cidx_server.db`: main application data
- `~/.cidx-server/groups.db`: groups and audit logs

Tables are migrated in dependency order to satisfy PostgreSQL foreign key constraints. The migration uses `INSERT ... ON CONFLICT DO NOTHING`, making it idempotent and safe to re-run.

JSON columns in SQLite (stored as text strings) are parsed and inserted as JSONB in PostgreSQL. Boolean columns stored as 0/1 integers in SQLite are converted to PostgreSQL `BOOLEAN`. Timestamp columns stored as Unix epoch floats (for some fields) are converted to ISO-8601.

Invocation:

```
python3 -m code_indexer.server.tools.migrate_to_postgres \
  --sqlite-path ~/.cidx-server/data/cidx_server.db \
  --groups-path ~/.cidx-server/groups.db \
  --pg-url postgresql://user:pass@host/db
```
