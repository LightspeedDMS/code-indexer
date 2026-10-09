# Maintenance Mode and Background Jobs

How a CIDX Server node stops accepting new work before a restart, how background jobs are listed and cancelled,
and what happens to jobs across restarts and cluster nodes.

## Maintenance mode

Maintenance mode makes a server process reject new background jobs while queries keep working. The auto-updater
enters it before every restart (see [Auto-Update](auto-update.md#restart-and-drain)).

### Endpoints

All under `/api/admin/maintenance` (`src/code_indexer/server/routers/maintenance_router.py`), all requiring an admin
token:

| Method and path | Extra restriction | Returns |
|-----------------|-------------------|---------|
| `POST /enter` | Loopback peers only | `maintenance_mode`, `running_jobs`, `queued_jobs`, `entered_at`, `message` |
| `POST /exit` | Loopback peers only | `maintenance_mode`, `message` |
| `GET /status` | - | `maintenance_mode`, `drained`, `running_jobs`, `queued_jobs`, `entered_at` |
| `GET /drain-status` | - | `drained`, `running_jobs`, `queued_jobs`, `estimated_drain_seconds`, `jobs` |
| `GET /drain-timeout` | - | `max_job_timeout_seconds`, `recommended_drain_timeout_seconds` |

`POST /enter` and `POST /exit` depend on `require_localhost`: the request's immediate peer address must be loopback
(`127.0.0.0/8`, `::1`, or an IPv4-mapped loopback), otherwise the server answers HTTP 403 `Localhost-only endpoint`.
They need no TOTP elevation, because their caller is the local auto-updater. Each switch writes an audit row
(`maintenance_mode_entered` / `maintenance_mode_exited`).

A reverse proxy or load balancer must not forward these two paths. The check sees only the immediate peer, and a
proxy on the same host connects from loopback, so a forwarded request would pass it. Refuse the paths at the proxy;
the nginx example in [Deployment](deployment.md#ports-and-network) does this.

There are no MCP tools to enter or exit maintenance mode. The MCP tool `get_maintenance_status` reports the state.
To enter maintenance by hand, call the endpoint from the node itself:

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/maintenance/enter -H "Authorization: Bearer $TOKEN"
curl -s http://127.0.0.1:8000/api/admin/maintenance/status -H "Authorization: Bearer $TOKEN"
curl -s -X POST http://127.0.0.1:8000/api/admin/maintenance/exit -H "Authorization: Bearer $TOKEN"
```

### What it does

- While on, job submission through `BackgroundJobManager`, the sync job manager and golden-repository add and
  remove raises `MaintenanceModeError`: `Server is in maintenance mode. New jobs are not accepted. Please retry
  after 60 seconds.`
- Queries and running jobs are not affected.
- The authenticated `/health` response includes `maintenance_mode`; maintenance mode does not change its `status`.

### Scope and limits

- The state lives in memory of the process that received `POST /enter` (`MaintenanceState`). It is not shared with
  other uvicorn workers on the same node or with other cluster nodes, and a restart clears it.
- `drained` and the job counts come only from job trackers registered with `MaintenanceState`, and no production
  code registers one. `drain-status` therefore reports `drained: true` with zero jobs even while jobs run. Do not
  use it to decide whether a restart is safe; check the jobs dashboard instead.
- The recommended drain timeout is 1.5 x `resource_config.git_refresh_timeout` (5400 seconds with the default).

## The jobs dashboard

The admin Web UI page `/admin/jobs` lists background jobs with filters for status, job type and a search string,
and offers cancellation (`POST /admin/jobs/{job_id}/cancel`, which requires step-up elevation when enforcement is
on).

The same data is available through the front doors:

| Front door | Operation |
|------------|-----------|
| `GET /api/jobs?status=&limit=&offset=` | List jobs (`limit` 1 to 100, default 10); admins see every user's jobs |
| `GET /api/jobs/{job_id}` | One job: status, progress, `current_phase`, `phase_detail`, result, error |
| `DELETE /api/jobs/{job_id}` | Cancel a pending or running job |
| `GET /api/admin/jobs/stats`, `DELETE /api/admin/jobs/cleanup` | Admin statistics and cleanup of old jobs |
| MCP `get_job_details`, `get_job_statistics`, `cancel_job` | Same operations over MCP |

Job statuses: `pending`, `running` (and `resolving_prerequisites`), then one terminal status: `completed`,
`completed_partial`, `failed`, `cancelled` or `interrupted`.

Finished jobs are deleted after `data_retention_config.background_jobs_retention_hours` (default 720 hours).

## Cancelling a job

`DELETE /api/jobs/{job_id}` (or the dashboard or MCP `cancel_job`) behaves as follows:

- **Pending job:** becomes `cancelled` immediately.
- **Running job on this process:** the job is flagged as cancelled and the cancellation is persisted. The job
  stops at its next cancellation check; subprocesses it started are stopped as described below. X-Ray jobs have
  their processes terminated at once.
- **Job owned by another process or node:** the cancellation is written to the shared job table. The owning
  process reads that flag through its database-backed cancel check (polled about every 2 seconds) and stops.
- **Dependency-map analysis:** a registered cancel handler stops the analysis workers and releases their shared
  sentinel.
- A job that is not pending or running cannot be cancelled (HTTP 400); a job of another user returns HTTP 403 for
  non-admins.

Golden-repository refreshes submitted through the background job manager bind their subprocesses to the job's
cancel check: git operations, the re-clone, `cidx init` repair, semantic and temporal indexing, SCIP generation,
snapshot preparation and the cidx-meta backup. A cancelled indexing subprocess's process group is terminated, and
survivors are killed after a grace period. Cancellation is also re-checked before indexing, after indexing, after
the integrity gate, before snapshot creation and before the alias swap; a snapshot created but not yet published
is scheduled for cleanup, and the job ends `cancelled`.

What cannot be interrupted:

- The copy-on-write copy that creates a snapshot runs to completion; the cancellation takes effect at the check
  before the alias swap.
- A refresh that a cluster node took over from a dead node (`execute_refresh_for_claimed_job`) runs without a
  cancel check; cancelling it does not stop its subprocesses.

A failed read of the cancel flag never stops a job. It is logged at WARNING on the first failure and every 30th
consecutive failure, and at ERROR once after 150 consecutive failures.

## No wall-clock timeouts on indexing

Indexing, golden-repository registration and SCIP generation have no job, subprocess or per-file timeout: a large
repository can legitimately index for hours. Only the individual outbound embedding HTTP calls have timeouts (with
retry). A long-running job is not a stuck job; judge progress from its phase and progress fields.

## Jobs across restarts

When the server stops, jobs that were running or pending in that process are left unfinished. At the next start
the server marks them `interrupted` (error text such as `orphaned - server restarted`). `interrupted` jobs are
restart artifacts: they do not count toward the failed-job total that makes `/health` report `degraded`.
Resubmit an interrupted job if its work is still needed; scheduled refreshes run again on their own schedule.

## Jobs in a cluster

- Each job records the node that runs it in `background_jobs.executing_node`.
- Duplicate submissions are prevented in the database: `register_job_if_no_conflict()` and the
  `idx_active_job_per_repo` unique index allow one active job per operation type and repository across all nodes.
- The leader node's `JobReconciliationService` (every 5 seconds) returns a `running` job to `pending` when its
  node has had no heartbeat for 30 seconds, so another node can claim it. A job running on a live node is never
  reclaimed by age.
- The leader's distributed job worker claims the oldest `pending` row cluster-wide; job types that cannot be
  retried are failed instead of re-run.

Details: [Cluster Setup](cluster-setup.md#jobs-across-nodes) and [Cluster Architecture](../architecture/cluster.md).
