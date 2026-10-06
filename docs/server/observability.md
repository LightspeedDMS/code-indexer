# Observability

How to tell whether a CIDX Server node is healthy, where its logs are, and what it exports to OpenTelemetry.

## Health endpoints

| Endpoint | Auth | Purpose | Response |
|----------|------|---------|----------|
| `GET /healthz` | none | Load-balancer liveness and readiness probe | `{"status": "healthy" \| "degraded" \| "unhealthy"}`; HTTP 200 for healthy and degraded, 503 for unhealthy |
| `GET /api/system/health` | token | Full system diagnosis | `status`, `timestamp`, `services`, `system`, `failure_reasons`, `audit`, `last_golden_repo_reconcile_auto_heal`, `fleet_migration_dedup_state` |
| `GET /health` | token | Server and job-queue summary | `status`, `message`, `uptime`, `active_jobs`, `job_queue`, `started_at`, `maintenance_mode`, `audit`, `version`, plus `system_resources`, `database`, `recent_errors` when available |

Routes: `src/code_indexer/server/routers/inline_misc.py`. The MCP tool `check_health` covers the same ground for MCP
clients.

### /healthz and /api/system/health

Both use the same computation (`HealthCheckService.get_system_health()` in
`src/code_indexer/server/services/health_service.py`). `/healthz` returns only the overall status, serves it from a
2-second per-process cache, and answers 503 `unhealthy` if the computation itself fails. `/api/system/health` is
uncached and lists up to three `failure_reasons` (then `+N more`).

The overall status is `unhealthy` if any check reports an error, otherwise `degraded` if any reports a warning,
otherwise `healthy`:

| Check | Degraded when | Unhealthy when |
|-------|---------------|----------------|
| RAM | 80 percent or more used | 90 percent or more used |
| CPU | above 95 percent for 30 seconds | above 95 percent for 60 seconds |
| Each mounted local volume (network and virtual filesystems such as `nfs`, `nfs4`, `cifs`, `smbfs`, `tmpfs`, `overlay` are skipped) | 80 percent or more used | 90 percent or more used |
| `storage` service: the filesystem holding the server data directory (`server_dir`) | 80 percent or more used | 90 percent or more used |
| Database connectivity (SQLite, or PostgreSQL in cluster mode) | response 1 to 5 seconds | response over 5 seconds, or connection failure |
| Server databases (per-database checks) | a database reports a warning | a database reports an error |
| Golden-repos directory readability (3-second probe) | - | the directory cannot be read (for example a dead NFS or CoW host) |
| SIEM delivery | delivery problem | never |
| Golden-repo registry reconcile circuit breaker, HNSW orphan sweep startup, fleet migration state | warning conditions | error conditions |

The RAM, CPU and disk percentages above are defaults. They come from the runtime `health_config` settings
(`memory_warning_threshold_percent`, `memory_critical_threshold_percent`, `disk_warning_threshold_percent`,
`disk_critical_threshold_percent`, `cpu_sustained_threshold_percent`), editable in the Web UI, Configuration,
Health Check Thresholds section. The health service reads them when it is created, so a change applies after the
next server restart.

Because network filesystems are skipped, a full NFS-mounted `/mnt/cow-storage` never degrades health on an NFS
client node; watch free space on the CoW daemon host itself. If that host also runs cidx-server, its health checks
cover that storage, because there it is a local or bind-mounted filesystem.

`unhealthy` drains the node from a load balancer that probes `/healthz`. Note that sustained RAM use at or above the
critical threshold (90 percent by default) is enough for that.

`last_golden_repo_reconcile_auto_heal` is informational (a past, resolved event) and never affects the status.
`fleet_migration_dedup_state` does affect it.

### /health

`/health` uses its own, simpler rules:

| `status` | Condition |
|----------|-----------|
| `degraded` | At least one job with status `failed` finished in the last 24 hours (`failed_jobs_window: "24h"`) |
| `warning` | More than 8 pending jobs |
| `healthy` | Otherwise |

Jobs marked `interrupted` by a restart do not count as failed. Maintenance mode is reported in `maintenance_mode`
but does not change `status`.

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<password>"}' | jq -r .access_token)
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/api/system/health -H "Authorization: Bearer $TOKEN" | jq '.status, .failure_reasons'
curl -s http://localhost:8000/health -H "Authorization: Bearer $TOKEN" | jq '.status, .version, .job_queue'
```

`GET /cache/stats` (token) reports the in-memory HNSW index cache: cached repositories, memory, hit and miss counts.

## Logs

### Where they are

- The process journal: `journalctl -u cidx-server` (startup output, uvicorn, anything written to stdout/stderr).
- The application log store. Every record at or above the configured level is written to:
  - standalone: the `logs` table of `~/.cidx-server/logs.db` (SQLite);
  - cluster: the `logs` table in PostgreSQL, shared by all nodes.

Each row has `timestamp`, `level`, `source`, `message`, `correlation_id`, `user_id`, `request_path`, `alias`,
`trace_id` and `span_id`.

The log level is the runtime setting `log_level` (Web UI, Configuration, Server section), applied at the next
restart (see [Auto-Update](auto-update.md#restart-requests-and-launch-settings)).

Operational logs older than `data_retention_config.operational_logs_retention_hours` (default 168 hours) are
deleted by the data retention job, which runs every `cleanup_interval_hours` (default 1).

### Reading them

- Web UI: `/admin/logs`, with filters for level, logger, search text and node; export as JSON or CSV from
  `/admin/logs/export`. Both require step-up elevation when enforcement is on.
- MCP (admin role, plus step-up elevation when enforcement is on): `admin_logs_query` (paginated, `page_size` up to 1000; filters `search`, `level`
  such as `ERROR,WARNING`, `correlation_id`) and `admin_logs_export` (`format` `json` or `csv`, same filters).
- Directly on a standalone node:

  ```bash
  sqlite3 ~/.cidx-server/logs.db \
    "SELECT timestamp, level, source, message FROM logs WHERE level IN ('ERROR','WARNING') ORDER BY id DESC LIMIT 50"
  ```

Audit events (logins, administrative changes) are a separate store, queried with the MCP tool `query_audit_logs`.

### Correlation and trace ids

Every HTTP request carries a correlation id: the server takes `X-Correlation-ID` from the request or generates a
UUID, returns it in the `X-Correlation-ID` response header, and stores it on every log row the request produces.
Filter by it (`correlation_id` in `admin_logs_query`, or the search box in the Web UI) to see one request's log
lines. When OpenTelemetry tracing is active, `trace_id` and `span_id` link each log row to its trace; rows written
with no active span carry a zero trace id.

## OpenTelemetry

Telemetry is off by default. Configure it in the Web UI, Configuration, OpenTelemetry Export section (runtime
settings); the UI marks enabling export as requiring a restart.

| Setting | Default | Meaning |
|---------|---------|---------|
| `enabled` | `false` | Master switch |
| `collector_endpoint` | `http://localhost:4317` | OTLP collector |
| `collector_protocol` | `grpc` | `grpc` or `http` |
| `service_name` | `cidx-server` | Service name on exported data |
| `export_traces` | `true` | Export spans |
| `export_metrics` | `true` | Export metrics |
| `export_logs` | `false` | Export log records over OTLP |
| `machine_metrics_enabled` | `true` | Host CPU, memory, disk and network gauges |
| `machine_metrics_interval_seconds` | `60` | Host metrics interval |
| `deployment_environment` | `development` | Environment attribute |
| `trace_sample_rate` | `1.0` | Fraction of new traces sampled (a sampled parent is always honoured) |

Metric instruments the server defines (code: `src/code_indexer/server/telemetry/` and the services named below):

| Area | Instruments |
|------|-------------|
| Search | `cidx.search.requests`, `cidx.search.duration`, `cidx.search.results_count` |
| Full-text search | `cidx.fts.requests`, `cidx.fts.duration`, `cidx.fts.matches` |
| Embedding provider calls | `cidx.embedding.requests`, `cidx.embedding.duration`, `cidx.embedding.tokens` |
| Background jobs | `cidx.jobs.active`, `cidx.jobs.queued`, `cidx.jobs.completed`, `cidx.jobs.failed`, `cidx.jobs.duration` |
| Repositories | `cidx.repos.total`, `cidx.repos.indexed`, `cidx.repos.refresh.duration` |
| Query-embedding cache | `cidx.cache.embedding.*` (`hits`, `misses`, `hit_rate`, `total_entries`, `provider_calls`, `write_failures`, ...) |
| SIEM delivery | `cidx.siem.*` |
| X-Ray | `cidx.xray.cache_identity_failures`, `cidx.xray.timeout_config_read_failures` |
| Host | `system.cpu.usage`, `system.memory.usage`, `system.disk.free`, `system.disk.io.read`, `system.disk.io.write`, `system.network.io.receive`, `system.network.io.transmit` |

In a cluster, exported data carries the node id as the resource attribute `cidx.cluster.node_id`.

## Cluster view

The admin dashboard shows a card per node from the `node_metrics` table (written every 5 seconds by each node) and
each node's role from `cluster_nodes`. See [Cluster Setup](cluster-setup.md#troubleshooting) for the SQL queries
behind leader, heartbeat and orphaned-job checks.
