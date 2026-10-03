# Background work: deciding where it runs

> The lifespan runs once per uvicorn **worker process**, not once per pod.
> On `tools-*` that is 6 replicas x `--workers 2` = **12 processes**, each with
> its own event loop, all pointed at one Postgres and one RWX NFS volume.
> Every concurrency number in this tree multiplies by 12, not by 6.

Getting this wrong is not loud. Duplicated background work shows up as memory
pressure, NFS latency and mysterious contention on the query path -- never as an
error attributable to the sweep that caused it.

## The five mechanisms in the tree

| # | Mechanism | Reference implementation | Primitive |
|---|---|---|---|
| **M1** | Leader gating | `server/startup/leader_gate.py` | Postgres leader election (`services/leader_election_service.py`) |
| **M2** | Pod-pull work queue | `services/index_job_claim_loop.py` + `services/distributed_job_claimer.py` | `UPDATE ... FOR UPDATE SKIP LOCKED ... RETURNING`, memory-gated |
| **M3** | Per-pod in-memory pool | `repositories/background_jobs.py` | none (node-local thread pool) |
| **M4** | Sentinel-row race-to-claim | `services/data_retention_scheduler.py` | partial unique index `idx_active_job_per_repo` on `(operation_type, repo_alias)` |
| **M5** | NFS lock files | `services/shared_job_sentinel.py`, `global_repos/write_lock_manager.py` | shared filesystem |

M4 is the most-used and the least documented: every process submits the same job,
one wins, eleven take `DuplicateJobError` and skip. Cheap and correct **when the
work is idempotent** -- it has a TOCTOU window, stated in-tree at
`services/fts_backfill/scheduler.py`. That window is why FTS backfill was promoted
from M4 to M1.

**M5 should not be used for new work.** It predates the Postgres primitives.

## Decision rule

Three questions, in order.

1. **Would two processes doing this simultaneously corrupt shared state, or do
   N x the work with no speedup?** -> **M1**, gate *inside* the loop so failover is
   picked up without a restart.
2. **Is it a set of independent per-item units where more pods = faster?** ->
   **M2**. Someone enqueues PENDING rows; every pod claims under the memory gate.
   Leader-gating divisible work is a mistake -- it serialises a fleet onto one pod.
3. **Otherwise it is node-local** (this pod's cache, this process's buffers, this
   node's metrics) -> **M3**, ungated, with any budget **divided by the uvicorn
   worker count** (see `xray/rust_backend.py` for the shape).

The producer/consumer split matters: M1 and M2 are not alternatives. The common
correct answer for a fleet-wide sweep of divisible work is *"the leader decides
what needs doing (M1), every pod does a share of it (M2)"* -- that is what the
semantic and FTS backfill sweeps do.

## Using the gate (M1)

```python
from code_indexer.server.startup.leader_gate import (
    await_sweep_leadership,      # one-shot startup work: WAITS for leadership
    resolve_sweep_leader,        # periodic loops: a single non-blocking check
)
```

**Periodic loop** -- check every cycle, never once at construction, so a failover
is picked up without a restart:

```python
while True:
    if resolve_sweep_leader(app.state):
        do_the_work()
    await asyncio.sleep(interval)
```

**One-shot startup sweep** -- must *wait*, because the process that is about to
become leader can reach the gate before it has won. Check once and the sweep runs
nowhere, on any pod, until the next restart:

```python
async def _run_sweep() -> None:
    try:
        if not await await_sweep_leadership(app.state, sweep_name="my sweep"):
            return
        ...
```

The default 120 s bound covers the two real delays -- the cluster block flipping
`scip_reconcile_leader_gate_ready` a few seconds into the same lifespan, and
`LeaderElectionService`'s 10 s monitor interval. Overshooting costs a sleeping
coroutine on the followers; undershooting costs a skipped sweep.

### Two properties worth knowing before you rely on it

**It fails open.** If the election cannot be consulted, every caller runs. Each
caller is a *self-heal* sweep, and before the gate existed they all ran on all 12
processes anyway -- so fail-open can only ever be as bad as the status quo, while
fail-closed would invent a new way for a fleet to quietly stop healing itself.

**Single-node is unchanged.** sqlite/solo mode has no election, so
`leader_election is None` means "leader" and `await_sweep_leadership` returns on
the first check without sleeping.

## R4: a node budget divides by the worker count

A limit written once in config is applied once **per worker process**. `5`
concurrent background jobs is 10 threads on a 2-worker pod and 60 across the
StatefulSet. `code_indexer/utils/worker_budget.py` does the arithmetic:

```python
from code_indexer.utils.worker_budget import detect_worker_count, per_process_budget

self._pool_size = per_process_budget(config.max_concurrent_background_jobs,
                                     detect_worker_count())
```

Three rules that are easy to get wrong:

- **Divide at the point of use, never in config.** The stored value is the node
  target an admin set and the Web UI reads back. Dividing it in `__post_init__`
  would show them 2 after they typed 5.
- **Store the result if two places need it.** `BackgroundJobManager` sizes its
  pool in `__init__` and drains it in `shutdown()` by sending one sentinel per
  worker. If those two ever disagree about the count, the surplus threads block
  on `get()` forever and the process does not exit. A test asserts `shutdown()`
  reads `self._pool_size` rather than recomputing.
- **Divide the limit, not the remainder.** `budget - active` divided by workers
  hands a process whose share is already full a budget of 1, leaking a slot every
  cycle.

Worker count is detected from two sources, larger wins: `get_applied_worker_count()`
(systemd/launch-json -- answers 1 in a pod, where it matters most) and `workers`
in the ConfigMap-seeded `config.json`. Unknown means **1**, i.e. no division and
today's behaviour. Nothing in that module raises; it runs at import in xray.

### Divided today

| Budget | Where |
|---|---|
| `_XRAY_COMPILE_BUDGET_PER_NODE` | `xray/rust_backend.py` (the original) |
| `max_concurrent_background_jobs` | `BackgroundJobManager.__init__` |
| `max_concurrent_refresh_jobs` | `RefreshScheduler._get_refresh_budget` |

### Not divided, on purpose

A constant that is not divided carries a comment saying why. Today:

- **`temporal_lane_concurrency`** (2) -- latency isolation, not a resource
  budget. Halving it on a 2-worker node leaves 1, defeating the separate lane's
  entire purpose. Pinned by a test.
- **`xray_max_concurrent_jobs`** (20) -- a job-slot ceiling in front of a gate
  that already divides. An xray job's real cost is the rustc compile, bounded
  per-process by `_MAX_CONCURRENT_COMPILES`.
- **`subprocess_max_workers`** (8) -- request burst capacity for short read-only
  ripgrep spawns, not a standing reservation. Two worker processes are not both
  bursting on the same query.

## Gating is not the same as acting on the gate

`ScheduledCatchupService` registered a job with `register_job` (no conflict
check) and swallowed the exception, so the work below ran on all 12 processes
regardless. The registration was dashboard decoration that *looked* like a claim.

A gate whose False branch falls through to the work is worse than no gate,
because it reads as handled. `tests/unit/server/startup/test_leader_gate.py`
parses `lifespan.py` and asserts both halves: that each shared-state startup
sweep calls the gate, **and** that it returns on the negative branch.

## Deliberately ungated

`_run_primary_lease_directory_bootstrap` is a bare `mkdir` of a directory that
`snapshot_reader_lease.py`'s own docstring promises exists before a node serves
traffic. Gating it would trade a harmless duplicate `mkdir` for a chance the
directory never appears. A test pins the exemption so removing it is a conscious
edit rather than something a later gating pass absorbs.

The periodic **registry reconcile** is likewise ungated on purpose: it is
idempotent and `IntegrityError`-tolerant by design, documented at its call site.

## Related

- [cluster-architecture.md](cluster-architecture.md) -- leader election, node heartbeats
- [git-seam-guard.md](git-seam-guard.md) -- the other fail-closed static contract in this tree
- [upstream-merge-corrections.md](upstream-merge-corrections.md) -- including the
  mistakes made while writing this
