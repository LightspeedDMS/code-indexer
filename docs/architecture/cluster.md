# Cluster Architecture

How CIDX Server runs as several nodes sharing one PostgreSQL database and one shared storage tree. Audience:
maintainers and contributors. Installing and operating a cluster (database, installer flags, load balancer,
troubleshooting queries) is in [Cluster Setup](../server/cluster-setup.md) and
[CoW Storage Setup](../server/cow-storage-setup.md); this page explains how the pieces work. Rules:
[Cluster State and Background Jobs](invariants/cluster-and-jobs.md),
[Shared Storage Protocol](invariants/shared-storage.md).

## Topology

```
              clients (Web UI, REST, MCP)
                        |
                  load balancer        health probe: GET /healthz
                 /      |      \
            node-1   node-2   node-3   same code, any node serves any request
                 \      |      /
       PostgreSQL               shared CoW storage
   (all shared state and          (one NFSv3 mount per node:
    coordination)                  golden-repos/, activated-repos/)
```

- `storage_mode` in `config.json` selects the mode: `sqlite` (default, single node) or `postgres` (cluster, requires
  `postgres_dsn`). Both are bootstrap keys; everything else configurable is a runtime setting stored in the database.
- Nodes do not talk to each other for coordination; every shared decision goes through PostgreSQL. Optional repository
  sharding forwards queries between nodes (see [Query serving and caches](#query-serving-and-caches)).
- Each node mounts the CoW daemon's storage once; `~/.cidx-server/data/golden-repos` and `activated-repos` are
  symlinks into it. The CoW daemon reflinks clones locally on its own filesystem.
- Session affinity is not required: cross-request state (search payloads, job status) is in the database.

## Storage abstraction

- Most persistent stores are a Protocol in `src/code_indexer/server/storage/protocols/` with a SQLite implementation
  (`server/storage/sqlite_backends/`) and a PostgreSQL implementation (`server/storage/postgres/`). Some keep both
  backends next to their service instead (for example `EmbeddingCallStatsPostgresBackend` in
  `server/services/embedding_call_stats.py`, `SearchEmbedEventPostgresBackend` in
  `server/services/search_embed_event_writer.py`).
  `StorageFactory.create_backends()` (`server/storage/factory.py`) returns a `BackendRegistry` for the configured mode.
  Application code depends on the Protocols only; construction sites obtain registry-backed stores through
  `resolve_backend_registry_attr`.
- PostgreSQL mode builds two pools: a general pool (`max_size` 20 by default) and a small critical pool (2 to 5
  connections) used by heartbeat and job reconciliation so HTTP and job traffic cannot starve them.
- At startup in PostgreSQL mode `MigrationRunner` applies pending migrations from
  `server/storage/postgres/migrations/sql/` (numbered `.sql` files, applied once each under an advisory lock), then
  creates the backends. Any failure aborts startup: a cluster node never falls back to local SQLite.
- Migrations are additive only, so nodes on adjacent versions can share the schema during a rolling update.

## Leader election

`LeaderElectionService` (`src/code_indexer/server/services/leader_election_service.py`):

- The leader holds the session-level advisory lock `0x434944585F4C4452` ("CIDX_LDR") on a dedicated connection, taken
  with `pg_try_advisory_lock`. PostgreSQL releases it when that connection closes, including on crash or network loss.
- A monitor thread runs every 10 s: the leader pings its connection with `SELECT 1` and steps down if it fails;
  followers try to acquire the lock.
- Dead-peer detection (`server/storage/postgres/dead_peer_detection.py`) sets client keepalives on the lock connection
  and server-side `tcp_keepalives_*` / `tcp_user_timeout` session settings through the libpq `options` parameter, so
  PostgreSQL drops a vanished leader's backend in about a minute instead of the OS default of hours. Values already in
  the operator's `postgres_dsn` are kept. This requires PostgreSQL 12 or later and direct connections (a pooler must
  pass `options` through).
- Leadership is not fenced: leader-only work already running may continue for a short time after another node takes
  over.

Services started only on the leader (`_on_become_leader` in `src/code_indexer/server/startup/lifespan.py`):

| Service | Role |
|---------|------|
| `JobReconciliationService` | returns jobs of dead nodes to the queue |
| `DistributedJobWorkerService` | claims and runs pending, reclaimed refresh jobs (`global_repo_refresh`, `refresh_golden_repo`) |
| `SelfMonitoringService` | scheduled log analysis |
| Langfuse trace sync | when `langfuse_config.pull_enabled` |

Everything else runs on every node, including the refresh scheduler, the description-refresh and dependency-map
schedulers, the HNSW orphan sweep and fleet migration. Those avoid duplicate work through the database: per-repository
job dedup (`register_job_if_no_conflict` and the `idx_active_job_per_repo` unique index), fixed sentinel aliases for
fleet-wide singletons, the DB-backed alias lock, and transaction-level advisory locks
(`pg_try_advisory_xact_lock`) around dependency-map scheduler decisions.

## Heartbeats and node identity

- `NodeHeartbeatService` (`server/services/node_heartbeat_service.py`) upserts the node's row in `cluster_nodes` every
  10 s with `role` `scheduler` (leader) or `worker`. A node is active when `status = 'online'` and its last heartbeat
  is at most 30 s old; on shutdown it marks itself `offline`.
- The node id comes from `cluster.node_id` through `resolve_cluster_node_id` (`server/utils/cluster_node_id.py`); the
  job tracker and the cluster services use the same resolver, so `background_jobs.executing_node` matches the
  heartbeat row.
- Shared secrets (JWT signing secret, MFA encryption key) live in the `cluster_secrets` table, created by the first
  node and read by the others.

## Jobs across nodes

- Pending jobs are claimed atomically with `FOR UPDATE SKIP LOCKED` (`DistributedJobClaimer`), stamping
  `executing_node` and `claimed_at`; later updates are guarded by `executing_node`, so a node never modifies another
  node's job.
- Memory-heavy index operations (`POD_PULL_OPS`: `add_golden_repo`, `provider_index_add`,
  `provider_temporal_index_rebuild`, `sync_repository`, `change_branch`) are pulled by whichever node has memory
  headroom (`IndexJobClaimLoop` on every node), and their progress is written to the shared row.
- Reconciliation (leader, every 5 s, `server/services/job_reconciliation_service.py`):
  - a `running` job whose `executing_node` is not active is reset to `pending`, after a grace period of three sweep
    intervals since it was claimed; when no node at all appears active the sweep reclaims nothing;
  - a malformed row (`running` with no `started_at`) older than `max_execution_time` (1800 s) is set to `failed`.
  - A running job on a live node is never reclaimed, however long it runs. Indexing has no wall-clock timeout.
- Cancelling a job owned by another node writes the cancellation to the database; the owner's cancel check sees it.

## Query serving and caches

- Every node can answer every query from the shared storage. Loaded HNSW graphs are cached per worker
  (`HNSWIndexCache`, capped per node and divided across workers); loaded id indexes are cached per worker in the
  id-index cache, which is limited by entry count (`CIDX_ID_INDEX_CACHE_MAX_ENTRIES`, default 200), not by size. Full-text queries do not use
  a cache: each one opens a fresh `TantivyIndexManager` on the index directory (`semantic_query_manager.py`,
  `routers/inline_query.py`, `multi/multi_search_service.py`), so a rewritten FTS index is seen at once.
- Global repositories are served from immutable snapshots; a refresh publishes a new snapshot path, so other nodes miss
  the cache and load the new index on their next query. Paths that are rewritten in place (the server temporal index)
  are revalidated by file identity on read. There is no cross-node invalidation message.
- With `cluster.sharding_enabled` (bootstrap, default false) each repository alias is owned by the top-ranked nodes of
  a rendezvous hash over the active nodes (`ShardOwnership`, `server/services/shard_ownership.py`); only owners keep
  its index cached, `ShardRouter` forwards single-repository queries to an owner (any routing failure serves
  locally), and `ShardPrewarmService` warms owned repositories.
  Ownership fails open to "every node owns everything".

## Node metrics

`NodeMetricsWriterService` (`server/services/node_metrics_writer_service.py`) runs on every node in both modes and
writes a system snapshot (CPU, memory, process RSS, swap, disk and network rates, volume usage, `server_version`)
every 5 s to `node_metrics`, deleting snapshots older than an hour. The dashboard shows the latest snapshot per node.

## From standalone to cluster

Converting a node (schema, offline data copy with `code_indexer.server.tools.migrate_to_postgres`, installer flags,
golden-repository move) is an operator procedure: [Cluster Setup](../server/cluster-setup.md#converting-a-standalone-node-to-cluster-mode).
