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

### Reading the worker count

Three sources, largest wins, unknown means 1 (no division):

| rung | in a pod | on a VM/laptop |
|---|---|---|
| `--workers N` from `/proc` | **works** | works |
| `get_applied_worker_count()` (systemd) | answers 1 | works |
| `workers` in `config.json` | **absent** | if the operator sets it |

The argv rung exists because the other two fail in the cluster, which is the
deployment the division was written for. This module once claimed config.json
was "the rung that works inside a Kubernetes pod, whose `workers` the overlay
keeps in lockstep with the `--workers` it passes in the container args". That
was never true: checked on a live pod, the container args say `--workers 2`
while `/data/config.json` has no `workers` key, so every budget divided by 1.
It shipped that way and the canary is what surfaced it -- every unit test
injected `detect_worker_count`, so none of them exercised the real detector.

The command line cannot drift from a parallel config key, because it *is* what
launched the process.

**The parent walk is required, not defensive.** uvicorn spawns workers through
multiprocessing, so a worker's own argv is
`python -B -c from multiprocessing.spawn import spawn_main; ...` and the flag
lives on the master. Observed live on pod-5: master pid 7 carries `--workers 2`;
workers pid 9 and 10 carry `spawn_main`, and reading only `/proc/self/cmdline`
returns None for both -- the processes that actually size the pools.

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

## Moving an op onto the pod-pull queue (M2)

Four edits, and three ways it can look done without being done.

1. Add the op to `POD_PULL_OPS` (`repositories/background_jobs.py`).
2. Add one executor to the dispatch table in `lifespan.py`. `validate_dispatch_covers`
   turns drift between the two into a startup `RuntimeError`, so these are a pair.
3. Pass `metadata=` on `submit_job` with everything the executing pod needs to
   reconstruct the work. **Without it the pod-pull branch falls through silently**
   to the submitting pod's local pool -- op in the set, executor present, and every
   job still runs where it was submitted.
4. Keep `func=` as the local-pool fallback. Solo/sqlite has no cluster to steal
   work across, and both routes run the same work.

### The executor must raise on a non-raising failure

`IndexJobClaimLoop` calls `complete_job(result)` for **any** return that does not
raise. An op whose worker reports failure as `{"success": False}` -- `_execute_refresh`
does, for an integrity-gate refusal -- is then recorded as a success, and the repo
quietly stops being processed. Test it with `result.get("success") is False`, not
`not result.get("success")`: an early-return shape that omits the key would
otherwise fail a job that succeeded, and then retry it every cycle.

### Every argument the submitter had must travel in the row

The executing pod cannot see the submitting call. `execute_refresh_for_claimed_job`
hardcoded `force_reset=False`, which was invisible while its only caller was the
leader re-executing a reclaimed row. Once an arbitrary pod claims the row, that is
also how an admin's force-reset arrives -- and a force-reset degraded to an ordinary
refresh does nothing at all, because change detection then skips an unchanged repo.

### What M2 does and does not give you

It gives the memory gate (`DistributedJobClaimer` gates **every** claim, not just
memory-heavy ops, so a pressured pod leaves rows for one with headroom) and
`FOR UPDATE SKIP LOCKED` fairness.

It does **not** currently give a cluster-wide concurrency ceiling. `DistributedJobClaimer`
has one (Bug #541, `max_concurrent_jobs`) but it is constructed without it, so it
defaults to 0 = disabled. Enabling it is a separate decision with its own blast radius.

## Gating is not the same as acting on the gate

`ScheduledCatchupService` registered a job with `register_job` (no conflict
check) and swallowed the exception, so the work below ran on all 12 processes
regardless. The registration was dashboard decoration that *looked* like a claim.

A gate whose False branch falls through to the work is worse than no gate,
because it reads as handled. `tests/unit/server/startup/test_leader_gate.py`
parses `lifespan.py` and asserts both halves: that each shared-state startup
sweep calls the gate, **and** that it returns on the negative branch.

## When M4's race is not enough

M4 (everyone submits, the partial unique index picks a winner) is cheap and
correct **when the work is idempotent**. It has a TOCTOU window, stated in-tree
at `services/fts_backfill/scheduler.py`, and whether that window matters is
entirely a property of the work:

| Work | M4 verdict |
|---|---|
| retention / reaper deletes | fine — a duplicate DELETE is a no-op |
| FTS backfill build | **promoted to M1** — rebuilds an index file |
| HNSW orphan repair | **promoted to M1** — reads an on-disk index and rewrites it, on shared NFS. A duplicate is two writers on one index |

Promoting keeps the M4 claim underneath. The gate removes the steady-state
storm; the claim still covers the failover moment when two processes briefly
both believe they are leader. They cost nothing together.

Three details that are easy to get wrong when promoting a scheduler:

- **Gate the loop, not the trigger.** `trigger_now()` is also the admin door.
  An operator asking *this* pod to sweep should get a sweep, not a silent no-op
  because another pod holds leadership.
- **A follower still heartbeats.** `HNSWOrphanRepairSweepScheduler` records
  `last_loop_cycle_at` and `last_cycle_skipped_reason` every cycle (Bug #1864)
  precisely so "switched off" and "thread died" are distinguishable. A follower
  is such a cycle: without its own `not_leader` reason, 11 of 12 pods report as
  dead to the admin endpoint. Keep the reasons distinct — *disabled* is an
  operator decision, *outside window* is a schedule, *not leader* is normal.
- **Followers poll leadership on a short cadence**, not the tick interval. This
  sweep's interval is in minutes *and* operating-window-gated, so a follower
  that slept a full interval could leave the fleet unswept until tomorrow.

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
