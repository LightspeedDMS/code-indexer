# ADR-003: Memory-governor integration for the X-Ray graph build (AC12-AC17)

Status: accepted
Date: 2026-09-06
Context: Epic #1786, Story #1787 (S2), amendment acceptance criteria AC12-AC17

## Context

The amendment to #1787 requires the whole-repo graph build (AC2-AC10) to be admitted,
bounded, and observed by the existing `MemoryGovernor`
(`src/code_indexer/server/services/memory_governor.py`), with an explicit constraint: **no
new governor API may be added**. At the time this amendment is implemented, three things
are simultaneously true that the amendment text does not resolve on its own:

1. `MemoryGovernor` is a Python class. Its `band` property and `admission_allowed()` method
   read process-local sampler state; there is no Rust binding to it and none should be
   added (that would be exactly the "new governor API" the amendment forbids, just moved to
   a different language).
2. `xray-core` is a Rust `[lib]` linked into the `xray-cli` `[[bin]]`, invoked by Python as a
   one-shot subprocess (`RustNativeBackend`, `rust_backend.py`). There is no PyO3/cdylib
   embedding of `xray-core` inside the server process, and no persistent graph-serving
   daemon. `graph_cache.rs` (AC9) is therefore an in-process Rust LRU that only outlives a
   single subprocess invocation's own lifetime.
3. `MemoryGovernor.attach_cache()` is single-slot (`self._attached_cache = cache`) and is
   already called once, in `service_init.py`, for the HNSW index cache. A second unguarded
   call would silently replace that registration and break HNSW's existing YELLOW eviction
   (test-pinned in `test_memory_governor_yellow_sampler_wiring.py`).

None of these are named as open questions in the story text, but each forces a decision
before AC12-AC17 can be implemented at all. Per the story's own instruction ("If you hit an
architectural fork, STOP and report rather than freelancing"), they are recorded here.

## Decision 1: Gate 1, Gate 2, phase-boundary checks, calibration, and observability all
live in Python

Because `MemoryGovernor` cannot be called from Rust without adding a new cross-language
governor surface, every AC12/AC13/AC16/AC17 decision point (gate math, `admission_allowed()`
calls, `band` reads, K calibration read/write, counter increments) is orchestrated from
Python. Rust's job is limited to (a) making the two measurement points genuinely available
before their corresponding expensive step runs, and (b) enforcing an OS-level ceiling on its
own child process once Python has computed one. Rust never decides admission; it only
produces the numbers Python's gates consume and enforces the limit Python computes.

## Decision 2: Gate 2 separability is a real code-structure split inside `bind`, not a
process-count decision

AC12 Gate 2 requires "the expensive allocation is separable from the measurement that
predicts it," proven by a discriminating test. The bind pipeline already had this
separation as an internal call sequence -- `bind_with_budget` (`bind/budget_bind.rs`) calls
`resolve_all_references()` to get an **exact** `total_candidates` count *before* calling
`CodeGraphBuilder::with_candidate_capacity(capacity)`, which is the one CSR-arena
allocation point (AC5's single-allocation invariant).

This story promotes that internal sequencing into an explicit public two-step API
(`bind/admission.rs`):

- `prepare_bind(files) -> (PreparedBind, PreBindStats)` -- runs extraction-adjacent work
  (name-index build, per-reference resolution) and returns exact
  `declaration_count`/`call_site_count`/`candidate_edge_count`, allocating **no** CSR
  arena.
- `finish_bind(prepared, budget) -> CodeGraph` -- the exact remainder of the old
  `bind_with_budget` body, starting at the CSR-arena allocation.
- `bind_with_admission_gate(files, budget, gate) -> BindOutcome` -- calls `prepare_bind`,
  then `gate(&stats)`; only calls `finish_bind` if the gate returns `true`. A caller can
  substitute `gate` with a closure that calls into the (Python-driven, out of process)
  admission decision by having the actual orchestrator invoke `xray-cli` in two stages
  (mirroring the AC7 second-process pattern already established): one invocation exposes
  `PreBindStats` before allocating, the next performs the allocation once the caller's
  external gate (Python, real `MemoryGovernor.admission_allowed()`) has approved it.
- `bind_with_budget` is now expressed as `finish_bind(prepare_bind(files).0, budget)` --
  zero behavioral drift, single source of truth, no duplicated ladder logic.

The discriminating test (`bind_with_admission_gate` denies) uses a `#[cfg(test)]`-only
allocation-attempt counter on `CodeGraphBuilder::with_candidate_capacity` (the same seam
style already used for Bug #1784's compile-cache identity test) rather than RSS
measurement: it is deterministic across CI environments, whereas an RSS-based assertion
would be sensitive to allocator/OS behavior this story does not need to depend on.

## Decision 3: AC15 containment is an injectable `MemoryCeiling`, degrading when
cgroup delegation is unavailable

Cgroup v2 `memory.max` containment for the analyze child requires creating a cgroup
directory under the current process's own delegated subtree and writing the child's pid
into `cgroup.procs`. This requires cgroup v2 delegation that is not guaranteed in every
environment (in particular, not guaranteed in this dev sandbox or in CI). Mirroring
`MemoryGovernor`'s own `_MemoryReaders` injection pattern (built for exactly the same
problem -- testing cgroup-dependent logic without requiring real cgroup permissions),
`analyze/process.rs` gains a `MemoryCeiling` trait with:

- `CgroupV2MemoryCeiling` -- the real implementation, used in production.
- A test-only fake used in `process.rs`'s own tests to simulate an OOM-killed child
  deterministically.

`run_analyze_child_with_memory_limit` degrades (proceeds without containment, logs, does
**not** abort the job) whenever `MemoryCeiling::create`/`add_pid` fails -- the estimate
being wrong is what AC15 exists to survive; containment setup itself failing must not
become a second way to lose the job. `AnalyzeStatus` gains a ninth, distinct variant,
`AbortedMemoryLimit`, set only when the ceiling reports a genuine OOM-kill
(`memory.events`'s `oom_kill` counter, never inferred from a bare SIGKILL exit alone --
a cancellation kill also uses SIGKILL and must not be misreported).

## Decision 4: AC14's "graph cache" is a Python-side LRU proxy over mmap-backed wire
files, composed into the governor's single attach slot

Given Decision 1 (Rust's `GraphCache` cannot be reached from the governor without a new
cross-language API) and the absence of a persistent graph-serving process (see Context,
point 2), the thing the governor can actually evict is **retention on the Python side**:
an LRU of `repo_snapshot_identity -> mmap'd wire-file handle` that the (future) query path
consults before re-invoking the Rust build pipeline, matching AC9's own framing ("served
from cache without re-extraction or re-bind"). `XrayGraphCacheProxy`
(`server/services/xray_graph_governor/cache_proxy.py`) implements the exact
`get_stats().cached_repositories` / `evict_lru_entries(n)` shape `evict_lru_to_floor()`
already expects (mirroring `HNSWIndexCache`'s contract).

Rather than changing `MemoryGovernor.attach_cache()`'s single-slot semantics (risking the
already-tested HNSW wiring, and arguably widening governor API surface the amendment
forbids touching), a `CompositeLRUCache` (`cache_governor_bridge.py`) wraps both the
existing HNSW cache and the new graph-cache proxy behind the ONE `get_stats()`/
`evict_lru_entries()` pair `attach_cache()` already accepts, and floors *each* sub-cache
independently at the same floor `evict_lru_to_floor()` already uses -- so HNSW's existing
YELLOW eviction behavior is byte-for-byte unchanged, and the graph cache now participates
under the identical policy. `governor.attach_cache(composite)` is still the literal call
AC14 requires; `memory_governor.py` itself is untouched by this decision.

## Decision 5: observability counters extend `GovernorCounters`/`get_snapshot()`, not a
new method surface

AC17 explicitly requires surfacing through "the existing governor stats path." This is
implemented exactly as Story #1600's own `query_admissions_denied` precedent: new fields on
`GovernorCounters`, echoed in `get_snapshot()`, incremented via one new thread-safe method
(`record_graph_build_admission_denied`, `record_graph_build_outcome`) guarded by the
existing `_counters_lock` -- not a parallel gating primitive, and not a second stats path.

## Consequences

- The full repo-build pipeline this integrates into (a real end-to-end `cidx-server` code
  path invoking `build_repo_graph` for a registered golden repo) does not exist yet in this
  codebase as of this story slice -- AC2-AC10's building blocks are library-level and
  tested in isolation, the same way AC7's `analyze/process.rs` and AC9's `graph_cache.rs`
  were built ahead of their own CLI/MCP wiring. AC12-AC17 are implemented here as the
  governor-integration layer those future call sites are required to use, fully tested at
  the unit/integration level given the seams above, but not exercised end-to-end through
  the MCP front door in this slice.
- Because Gate 1/Gate 2 are Python-side and the actual build pipeline invocation is still a
  future integration point, the two-subprocess split described in Decision 2 is a
  structural capability (provable today) rather than a wired `xray-cli` subcommand pair --
  wiring `xray-cli`'s CLI surface to expose `prepare_bind`/`finish_bind` as two distinct
  invocations is left to the story slice that wires the full repo-build pipeline to the
  server, consistent with how `--analyze-graph` (AC7/AC8) was added only once its own
  consumer was ready.
