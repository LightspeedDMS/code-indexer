"""Story #1491: REAL concurrent-request proofs that the previously-blocking
server paths no longer stall the shared asyncio event loop.

The story's Testing Requirements are explicit and binding:

    "For each of AC1-AC5, a REAL concurrent test: issue the previously-blocking
     request/operation and, while it is in flight, issue a second request and
     assert it is served promptly. Real server (FastAPI TestClient or live
     uvicorn), real handlers -- a mock-only unit test is NOT acceptable
     evidence for this story."
    "Record measured second-request latency during the blocking operation,
     before and after."

Concurrency approach (mirrors the pattern this repo already established in
tests/e2e/server/test_13_depmap_coordination_1133.py): a real FastAPI app +
``httpx.AsyncClient`` over ``httpx.ASGITransport`` + ``asyncio.gather``.  Both
requests land on the SAME event loop, concurrently, exactly as two real HTTP
clients hitting one uvicorn worker do.

Why NOT ``TestClient`` for the concurrency half: Starlette's ``TestClient``
drives the app through a single ``anyio.BlockingPortal``; ``portal.call()`` is
synchronous, so two threads issuing requests through one TestClient SERIALISE
on the portal and can never demonstrate (or refute) event-loop blocking.

What is real and what is controlled
-----------------------------------
The mechanism under test in every case below -- FastAPI/Starlette sync-vs-async
dispatch, the MCP protocol dispatcher's executor offload, Starlette's
sync-vs-async BackgroundTask handling, and ``anyio.to_thread`` offload -- is
FULLY REAL and never mocked, and the production functions under test are the
real ones imported from ``code_indexer.server``.

For AC1 the blocking cost is REAL bcrypt (``bcrypt.checkpw`` against a real
bcrypt hash), because "bcrypt cost is the point" per the story.

For AC2/AC3/AC4 the innermost external operation (a ripgrep run, a
``git ls-remote`` subprocess, an external-API diagnostic) is replaced at a real
production seam by a stand-in that performs a REAL blocking
``time.sleep(_SLOW_SECONDS)`` and returns a realistically-shaped value.  This
is deliberate: those operations' own duration is environment-dependent
(network reachability, corpus size), and what these tests must measure is
WHERE the blocking work runs, not how long a real remote takes.  A
deterministic, real, synchronous block of known duration is the correct
instrument for that question -- if the work still ran on the event loop, the
concurrent probe request would be delayed by ~_SLOW_SECONDS; if it is offloaded,
the probe is served in single-digit milliseconds.  Per-repo/per-category error
and result semantics are covered separately by each path's own pre-existing
test suites, which are unchanged by this story.

Measurements are recorded to reports/perf/ following the shape Story #1493
established in reports/perf/temporal_overfetch_1493_ac4_concurrency_results.json.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from code_indexer.server.services.diagnostics_service import (
        DiagnosticCategory,
        DiagnosticResult,
        DiagnosticsService,
    )

logger = logging.getLogger(__name__)

# The User objects built below are never authenticated against a real store --
# the handlers under test only read .username. This value is a non-secret
# placeholder, never a real or usable password hash.
_PLACEHOLDER_PASSWORD_HASH = "not-a-real-hash"

# How long each stand-in blocking unit of work takes.  Large enough that a
# blocked event loop is unmistakable (>= 2 orders of magnitude above the
# promptness bar), small enough to keep the suite fast.
_SLOW_SECONDS = 0.6

# A second request served while the slow operation is in flight must come back
# well under this.  A blocked loop would make it >= _SLOW_SECONDS.
_PROMPT_LATENCY_BUDGET_SECONDS = 0.25

# Shared by the two before/after comparisons (AC1's bcrypt and AC2's regex
# dispatch): the post-change probe must be dramatically faster than the
# pre-change one, not merely under an absolute budget, since the real work's
# duration is machine-dependent.
_MIN_PROBE_IMPROVEMENT_RATIO = 4.0

# AC2's own, tighter promptness bar. regex_search's synchronous share is real
# work of a few hundred milliseconds rather than a fixed _SLOW_SECONDS block, so
# AC2 measures against this instead of the shared budget above; it is still more
# than an order of magnitude above the single-digit-millisecond latencies the
# offloaded path actually produces.
# Raised 0.05 -> 0.1 per dual-review item 9: this project documents test
# flakiness under concurrent gate load, and the offloaded path measures ~2-5ms,
# so 0.1s is still an order of magnitude of headroom while removing a needless
# flake risk. This value governs ONLY the fixed path's promptness; the
# anti-vacuity floor below is deliberately independent of it.
_AC2_PROBE_BUDGET_SECONDS = 0.1

# Measured behaviour of this AC2 scenario on real hardware, over 20 individually
# recorded runs on a 12-core host (6 idle, 14 alongside five concurrent pytest
# processes):
#   * pre-change async dispatch stalled the loop for 0.3906s .. 0.5210s
#   * post-change sync dispatch answered probes in 0.0024s .. 0.0084s
# Note the direction of the load effect on each: the stall GROWS under load
# (more CPU contention inside the blocking section), so the risk to the floor
# below is a fast, idle machine -- which is exactly where the 0.3906s minimum
# was observed. Recorded so the floor has a stated empirical basis, not a taste.
_AC2_OBSERVED_MIN_BASELINE_STALL_SECONDS = 0.39
_AC2_OBSERVED_MAX_OFFLOADED_PROBE_SECONDS = 0.009

# Anti-vacuity guard for AC2 (review item 2): before trusting ANY before/after
# comparison, the pre-change baseline must be shown to genuinely stall the loop
# for at least this long. Without it, a corpus too small to block would let the
# comparison "pass" against completely unfixed code -- which is exactly what
# both reviewers demonstrated on an earlier revision.
#
# Round 4: this was `4 * _AC2_PROBE_BUDGET_SECONDS`, which made it a hostage of
# an unrelated constant -- raising the budget to 0.1 moved the floor to 0.4s,
# inside the measured 0.39-0.45s stall band, and the test began failing roughly
# one run in six. It is now an independent literal describing how long the REAL
# blocking section takes, which is what "did the baseline genuinely stall"
# actually depends on. 0.15s is ~2.6x below the slowest-case minimum observed
# above and ~33x above the offloaded path, so it cannot be tripped by noise and
# cannot be satisfied by an already-fixed path.
_AC2_MIN_BASELINE_STALL_SECONDS = 0.15

# ASGITransport requires a syntactically valid base URL; no socket is opened
# and no name is resolved, so this host never leaves the process.
_TEST_BASE_URL = "http://testserver"

# RFC 2606 reserves .invalid so these clone URLs can never resolve to a real
# host even if a future change accidentally let a real subprocess run.
_UNRESOLVABLE_CLONE_URL_TEMPLATE = "https://example.invalid/org/repo{index}.git"

_PERF_ARTIFACT = (
    Path(__file__).resolve().parents[3]
    / "reports"
    / "perf"
    / "event_loop_blocking_1491_concurrency_results.json"
)

# Measurements are accumulated in-process and rewritten to the artifact after
# every test, so the artifact is complete even when a subset of these tests
# runs in isolation and a later failure never discards an earlier real
# measurement within the same process.  The lock guards same-process
# interleaving only -- the artifact is a human-readable evidence record, not a
# correctness-bearing store, and no cross-process write coordination is
# claimed or attempted.
_MEASUREMENTS: Dict[str, Dict[str, object]] = {}
_MEASUREMENTS_LOCK = threading.Lock()

# The measured/derived numbers recorded per test: ints, floats, and lists of
# floats.  Declared explicitly rather than as Any so a typo in a payload key's
# value type is caught by mypy.
MeasurementValue = Union[int, float, List[float]]

# The slow request under test: given an open client, issue it and return the
# response.
SlowRequest = Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]


def _record(key: str, payload: Dict[str, MeasurementValue]) -> None:
    """Accumulate a measurement and REPLACE the perf artifact.

    Deliberately replace-not-merge. An earlier version merged this session's
    measurements into whatever the file already held, which meant a key written
    by a test that has since been deleted or renamed survived indefinitely --
    the artifact then advertised evidence for a test that no longer exists,
    which reads as fabricated. Writing only what this session actually measured
    makes the artifact self-consistent by construction: every key in it was
    produced by a test that ran, in the run that produced the file.

    Consequence worth knowing: running a SUBSET of these tests rewrites the
    artifact with only that subset. Regenerate by running the whole file.
    """
    with _MEASUREMENTS_LOCK:
        _MEASUREMENTS[key] = dict(payload)
        _PERF_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        _PERF_ARTIFACT.write_text(json.dumps(_MEASUREMENTS, indent=2, sort_keys=True))


def _app_with_probe() -> FastAPI:
    """A real FastAPI app carrying the trivial async /ping probe route.

    /ping is a pure-async no-op: the ONLY thing that can delay it is the event
    loop itself being unavailable.  It is therefore a direct measurement of
    event-loop responsiveness under concurrent load.
    """
    app = FastAPI()

    @app.get("/ping")
    async def _ping() -> JSONResponse:  # pragma: no cover - trivial probe
        return JSONResponse(content={"pong": True})

    return app


class _BlockingBarrier:
    """The instant the slow operation ENTERS its blocking section.

    Story #1491 review item 16: probe timing must not depend on a fixed timing
    stagger.  A staggered probe is a lottery -- if the blocking section happens
    to start after the probes fired, an unfixed (still-blocking) code path
    measures fast and the comparison silently proves nothing.  This is the
    explicit synchronisation point instead: the operation under test signals
    ``enter()`` at the top of the section that must not run on the event loop,
    and every probe waits for that signal and measures from the recorded
    instant.

    The recorded instant is what makes it work in BOTH directions: when the
    work is still on the loop, the probes' own polling sleep cannot resume
    until the block ends, so they report the full residual stall; when the work
    is offloaded, they observe the signal immediately and report milliseconds.

    ``enter()`` may be called from either the event-loop thread or a worker
    thread, and only the FIRST call is recorded (a section entered repeatedly
    within one measurement is anchored at its first entry).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._entered_at: Optional[float] = None

    def enter(self) -> None:
        with self._lock:
            if self._entered_at is None:
                self._entered_at = time.perf_counter()
        self._event.set()

    def is_entered(self) -> bool:
        return self._event.is_set()

    def entered_at(self) -> float:
        with self._lock:
            assert self._entered_at is not None, "barrier was never entered"
            return self._entered_at


# Poll interval for a probe waiting on the barrier. Small relative to the
# promptness budget so the wait itself contributes negligible latency.
_BARRIER_POLL_SECONDS = 0.005

# Bounded wait (Messi Rule #14): if the slow operation never signals its
# blocking section, fail loudly rather than hang.
_BARRIER_WAIT_TIMEOUT_SECONDS = 60.0


async def _measure_probe_latency_during(
    app: FastAPI,
    slow_request: SlowRequest,
    *,
    barrier: _BlockingBarrier,
    probe_count: int = 3,
) -> Dict[str, object]:
    """Run ``slow_request`` and concurrently issue ``probe_count`` /ping calls.

    Returns the slow request's response plus the measured per-probe latencies.

    Every probe waits for ``barrier`` -- the slow operation's own signal that it
    has entered the section that must not execute on the event loop -- and then
    measures from the instant the barrier recorded, NOT from when its polling
    sleep happened to resume.  That distinction is essential: when the loop is
    blocked, the starvation is absorbed inside the sleep's own overrun, so a
    timer started after the sleep would report a fast probe against a
    completely frozen loop.
    """
    if probe_count < 1:
        raise ValueError(f"probe_count must be >= 1, got {probe_count}")

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url=_TEST_BASE_URL
    ) as client:
        probe_latencies: List[float] = []

        async def _probe(ordinal: int) -> None:
            deadline = time.perf_counter() + _BARRIER_WAIT_TIMEOUT_SECONDS
            while not barrier.is_entered():
                assert time.perf_counter() < deadline, (
                    "the slow operation never signalled its blocking section"
                )
                await asyncio.sleep(_BARRIER_POLL_SECONDS)
            entered_at = barrier.entered_at()
            resp = await client.get("/ping")
            probe_latencies.append(time.perf_counter() - entered_at)
            assert resp.status_code == 200

        slow_started = time.perf_counter()
        results = await asyncio.gather(
            slow_request(client),
            *[_probe(i) for i in range(probe_count)],
        )
        slow_elapsed = time.perf_counter() - slow_started

    return {
        "slow_response": results[0],
        "slow_elapsed_s": slow_elapsed,
        "probe_latencies_s": probe_latencies,
        "max_probe_latency_s": max(probe_latencies),
    }


def _slow_response(measured: Dict[str, object]) -> httpx.Response:
    resp = measured["slow_response"]
    assert isinstance(resp, httpx.Response)
    return resp


def _max_probe_latency(measured: Dict[str, object]) -> float:
    value = measured["max_probe_latency_s"]
    assert isinstance(value, float)
    return value


def _total_wall(measured: Dict[str, object]) -> float:
    value = measured["slow_elapsed_s"]
    assert isinstance(value, float)
    return value


def _probe_latencies(measured: Dict[str, object]) -> List[float]:
    value = measured["probe_latencies_s"]
    assert isinstance(value, list)
    return value


def _assert_probe_was_prompt(measured: Dict[str, object], what: str) -> None:
    max_probe = _max_probe_latency(measured)
    assert max_probe < _PROMPT_LATENCY_BUDGET_SECONDS, (
        f"a concurrent request was delayed while {what} ran -- the blocking "
        f"work is still on the event loop (max probe latency {max_probe:.3f}s)"
    )


class _NoStoredTokens:
    """Stand-in CI token manager: no stored platform credentials.

    Credential retrieval is orthogonal to the event-loop question under test.
    """

    def get_token(self, platform: str) -> None:
        return None


# ===========================================================================
# AC3 -- branch discovery (server/web/routes.py::fetch_discovery_branches)
# ===========================================================================


def _install_slow_branch_fetch(
    monkeypatch: pytest.MonkeyPatch, barrier: _BlockingBarrier
) -> None:
    """Replace the real git ls-remote subprocess with a known-duration block."""
    from code_indexer.server.services import remote_branch_service as rbs_module

    def _slow_fetch(
        service_self: rbs_module.RemoteBranchService,
        clone_url: str,
        platform: str = "github",
        credentials: Optional[str] = None,
    ) -> rbs_module.BranchFetchResult:
        # A REAL synchronous block of known duration standing in for the real
        # git ls-remote subprocess (see module docstring). The barrier marks the
        # exact instant the block starts, so probes are anchored to it rather
        # than to a guessed stagger.
        barrier.enter()
        time.sleep(_SLOW_SECONDS)
        return rbs_module.BranchFetchResult(
            success=True,
            branches=[f"main-{clone_url}"],
            default_branch="main",
            error=None,
        )

    monkeypatch.setattr(
        rbs_module.RemoteBranchService, "fetch_remote_branches", _slow_fetch
    )


def _branch_discovery_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A real app exposing the REAL fetch_discovery_branches route + /ping.

    The route's auth/elevation dependency is intentionally omitted (auth is
    orthogonal to event-loop behaviour); the route body itself is unmodified.
    """
    from code_indexer.server.web import routes as routes_module

    monkeypatch.setattr(
        routes_module, "_require_admin_session", lambda request: {"username": "admin"}
    )
    monkeypatch.setattr(routes_module, "_get_token_manager", _NoStoredTokens)

    app = _app_with_probe()
    app.post("/api/discovery/branches")(routes_module.fetch_discovery_branches)
    return app


def _assert_branch_discovery_body(resp: httpx.Response, clone_urls: List[str]) -> None:
    """The response contract must be identical to the pre-change behaviour."""
    assert resp.status_code == 200
    body = resp.json()
    for url in clone_urls:
        assert body[url] == {
            "branches": [f"main-{url}"],
            "default_branch": "main",
            "error": None,
        }
    assert body["{'platform': 'github'}"] == {
        "branches": [],
        "default_branch": None,
        "error": "Missing clone_url",
    }


@pytest.mark.asyncio
async def test_ac3_branch_discovery_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N slow remote-branch fetches must not stall the loop, nor serialise.

    Before this story ``fetch_discovery_branches`` was an ``async def`` route
    that called the synchronous ``RemoteBranchService.fetch_remote_branches``
    (``subprocess.run(["git","ls-remote",...], timeout=30)``) in a plain
    sequential ``for`` loop -- N unreachable remotes froze the whole event loop
    for N x 30 s.  Two independent properties are asserted here:

    1. A concurrent /ping is served promptly (the loop is free).
    2. Total wall time is close to ONE slow unit, not N -- i.e. the fetches run
       concurrently (bounded), not sequentially.
    """
    repo_count = 4
    barrier = _BlockingBarrier()
    _install_slow_branch_fetch(monkeypatch, barrier)
    app = _branch_discovery_app(monkeypatch)

    clone_urls = [
        _UNRESOLVABLE_CLONE_URL_TEMPLATE.format(index=i) for i in range(repo_count)
    ]
    payload = {
        "repos": [{"clone_url": url, "platform": "github"} for url in clone_urls]
        + [{"platform": "github"}]  # missing clone_url -> error branch
    }

    async def _slow(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/api/discovery/branches", json=payload)

    measured = await _measure_probe_latency_during(app, _slow, barrier=barrier)
    _assert_branch_discovery_body(_slow_response(measured), clone_urls)

    total_wall = _total_wall(measured)
    _record(
        "ac3_branch_discovery",
        {
            "repo_count": repo_count,
            "per_repo_block_s": _SLOW_SECONDS,
            "sequential_on_loop_expectation_s": repo_count * _SLOW_SECONDS,
            "measured_total_wall_s": total_wall,
            "measured_probe_latencies_s": _probe_latencies(measured),
            "measured_max_probe_latency_s": _max_probe_latency(measured),
            "probe_budget_s": _PROMPT_LATENCY_BUDGET_SECONDS,
        },
    )

    _assert_probe_was_prompt(measured, "branch discovery")
    # Bounded concurrency: N fetches must not serialise into N x block.
    assert total_wall < repo_count * _SLOW_SECONDS * 0.75, (
        "branch discovery still fans out sequentially (total wall "
        f"{total_wall:.3f}s for {repo_count} x {_SLOW_SECONDS}s)"
    )


# A path that cannot be a git repository, used to make a REAL `git ls-remote`
# subprocess fail fast and deterministically without touching the network.
_NONEXISTENT_LOCAL_REPO_PATH = "/nonexistent/definitely-not-a-repo-1491.git"

# Bound for that real subprocess: it fails immediately, so this is only a
# safety net against a hung git, never a value the test waits on.
_REAL_GIT_PROBE_TIMEOUT_SECONDS = 10

# How long each counted subprocess stays "in flight" so overlapping calls are
# observable. Short: only the PEAK overlap count matters, not the duration.
_CONCURRENCY_OBSERVATION_WINDOW_SECONDS = 0.05

# A minimal but real `git ls-remote` stdout payload, so the production parsing
# path (branch extraction + default-branch detection) runs for real.
_LS_REMOTE_STDOUT = (
    "1111111111111111111111111111111111111111\tHEAD\n"
    "1111111111111111111111111111111111111111\trefs/heads/main\n"
)


def test_ac3_unreachable_remote_error_semantics_preserved() -> None:
    """A genuinely unreachable remote still yields the same per-repo error.

    Uses the REAL RemoteBranchService against a real, definitively-nonexistent
    local path, so a real ``git ls-remote`` subprocess actually runs and fails.
    Proves the AC3 concurrency change did not alter per-remote error reporting.
    """
    from code_indexer.server.services.remote_branch_service import RemoteBranchService

    service = RemoteBranchService(timeout=_REAL_GIT_PROBE_TIMEOUT_SECONDS)
    result = service.fetch_remote_branches(
        clone_url=_NONEXISTENT_LOCAL_REPO_PATH,
        platform="github",
        credentials=None,
    )
    assert result.success is False
    assert result.branches == []
    assert result.default_branch is None
    assert result.error is not None


class _CompletedProcessStub:
    """Stand-in for subprocess.CompletedProcess with a real ls-remote payload."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = _LS_REMOTE_STDOUT
        self.stderr = ""


@pytest.mark.asyncio
async def test_ac3_branch_discovery_concurrency_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The introduced concurrency must be BOUNDED (explicit AC3 requirement).

    Drives a repo list LARGER than the cap through the real route and counts
    overlap at the OS-subprocess boundary -- the only thing replaced here is
    ``subprocess.run`` (a genuinely external dependency); the whole production
    stack above it, including ``RemoteBranchService.fetch_remote_branches``'s
    real argument building, output parsing and result construction, runs for
    real. Asserts the peak number of simultaneous git invocations never exceeds
    the configured bound, while still being greater than one.
    """
    import subprocess

    from code_indexer.server.web import routes as routes_module

    cap = routes_module._DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY
    repo_count = cap + 5
    counter_lock = threading.Lock()
    in_flight = 0
    peak = 0

    def _counting_run(*args: object, **kwargs: object) -> _CompletedProcessStub:
        nonlocal in_flight, peak
        with counter_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            time.sleep(_CONCURRENCY_OBSERVATION_WINDOW_SECONDS)
        finally:
            with counter_lock:
                in_flight -= 1
        return _CompletedProcessStub()

    monkeypatch.setattr(subprocess, "run", _counting_run)
    app = _branch_discovery_app(monkeypatch)
    payload = {
        "repos": [
            {
                "clone_url": _UNRESOLVABLE_CLONE_URL_TEMPLATE.format(index=i),
                "platform": "github",
            }
            for i in range(repo_count)
        ]
    }

    async def _drive() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL
        ) as client:
            return await client.post("/api/discovery/branches", json=payload)

    resp = await _drive()
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == repo_count
    for entry in body.values():
        assert entry["branches"] == ["main"]
        assert entry["error"] is None
    assert peak <= cap, (
        f"branch discovery fanned out {peak} concurrent git invocations, "
        f"exceeding the bounded cap of {cap}"
    )
    assert peak > 1, "no concurrency observed at all -- fetches are sequential"


@pytest.mark.asyncio
async def test_ac3_branch_discovery_concurrency_bound_is_process_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound must hold across CONCURRENT REQUESTS, not just within one.

    Review item 11: a bound created per request is not a bound. AC3 requires the
    introduced concurrency to be BOUNDED so a large auto-discovery list can never
    burst git subprocesses; if the limiter is constructed inside the route, K
    concurrent admin requests permit cap x K simultaneous ``git ls-remote``
    processes -- the exact fan-out the requirement exists to prevent.

    Drives TWO concurrent requests through the REAL route, each carrying a full
    cap's worth of repos, and counts overlap at the OS-subprocess boundary (the
    only thing replaced; the whole production stack above it runs for real).
    """
    import subprocess

    from code_indexer.server.web import routes as routes_module

    cap = routes_module._DISCOVERY_BRANCH_FETCH_MAX_CONCURRENCY
    counter_lock = threading.Lock()
    in_flight = 0
    peak = 0

    def _counting_run(*args: object, **kwargs: object) -> _CompletedProcessStub:
        nonlocal in_flight, peak
        with counter_lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            time.sleep(_CONCURRENCY_OBSERVATION_WINDOW_SECONDS)
        finally:
            with counter_lock:
                in_flight -= 1
        return _CompletedProcessStub()

    monkeypatch.setattr(subprocess, "run", _counting_run)
    app = _branch_discovery_app(monkeypatch)
    payload = {
        "repos": [
            {
                "clone_url": _UNRESOLVABLE_CLONE_URL_TEMPLATE.format(index=i),
                "platform": "github",
            }
            for i in range(cap)
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL
    ) as client:
        first, second = await asyncio.gather(
            client.post("/api/discovery/branches", json=payload),
            client.post("/api/discovery/branches", json=payload),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert peak <= cap, (
        f"two concurrent discovery requests fanned out {peak} simultaneous git "
        f"invocations against a cap of {cap} -- the concurrency bound is "
        "per-request, so it does not actually bound the process"
    )
    assert peak > 1, "no concurrency observed at all -- fetches are sequential"


class _OutstandingTaskCounter:
    """Pass-through proxy around the REAL pool, tallying unfinished tasks.

    Counts at the actual submission boundary: every task the route hands to the
    pool is outstanding from the moment it is submitted until its future
    completes, which is exactly the population an unbounded work queue lets grow
    without limit.  Nothing about execution is altered -- the real shared pool
    still runs every task.
    """

    def __init__(self, real_pool: Any) -> None:
        self._real_pool = real_pool
        self._lock = threading.Lock()
        self._outstanding = 0
        self.peak = 0

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._outstanding += 1
            self.peak = max(self.peak, self._outstanding)
        future = self._real_pool.submit(fn, *args, **kwargs)
        future.add_done_callback(self._finished)
        return future

    def _finished(self, _future: Any) -> None:
        with self._lock:
            self._outstanding -= 1


def _install_outstanding_task_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> _OutstandingTaskCounter:
    """Route discovery submissions through a counting proxy over the real pool."""
    from code_indexer.server.web import routes as routes_module

    counter = _OutstandingTaskCounter(
        routes_module._get_discovery_branch_fetch_executor()
    )
    monkeypatch.setattr(
        routes_module, "_get_discovery_branch_fetch_executor", lambda: counter
    )
    return counter


def _slow_stub_run(*args: object, **kwargs: object) -> _CompletedProcessStub:
    """A git ls-remote stand-in slow enough for overlap to be observable."""
    time.sleep(_CONCURRENCY_OBSERVATION_WINDOW_SECONDS)
    return _CompletedProcessStub()


@pytest.mark.asyncio
async def test_ac3_branch_discovery_submission_queue_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submission itself must backpressure -- a bounded pool is not enough.

    Dual-review round 4: the shared pool bounds how many ls-remote fetches RUN
    at once, but ``ThreadPoolExecutor``'s internal work queue is an unbounded
    ``SimpleQueue``.  A single large discovery list (or several concurrent ones)
    therefore enqueued an unlimited number of pending fetch tasks -- work
    accepted with no limit and no signal, growing memory without bound.  The
    route must instead refuse to SUBMIT beyond a fixed process-wide outstanding
    budget, making excess callers wait (in async space, holding no thread and no
    queue slot) until a slot frees.

    Nothing in the production path is replaced except ``subprocess.run`` (a
    genuinely external dependency) and the accessor handing back the shared
    pool, which returns a pass-through counting proxy around the REAL pool.
    """
    import subprocess

    from code_indexer.server.web import routes as routes_module

    outstanding_cap = routes_module._DISCOVERY_BRANCH_FETCH_MAX_OUTSTANDING
    repo_count = outstanding_cap * 3
    counter = _install_outstanding_task_counter(monkeypatch)
    monkeypatch.setattr(subprocess, "run", _slow_stub_run)

    app = _branch_discovery_app(monkeypatch)
    payload = {
        "repos": [
            {
                "clone_url": _UNRESOLVABLE_CLONE_URL_TEMPLATE.format(index=i),
                "platform": "github",
            }
            for i in range(repo_count)
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL
    ) as client:
        resp = await client.post("/api/discovery/branches", json=payload)

    # Backpressure, not rejection: every repo is still fetched and reported.
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == repo_count
    for entry in body.values():
        assert entry["branches"] == ["main"]
        assert entry["error"] is None

    assert counter.peak <= outstanding_cap, (
        f"{repo_count} repos put {counter.peak} fetch tasks into the shared "
        f"pool at once, over the outstanding budget of {outstanding_cap} -- "
        "submission is unbounded, so the pool's internal queue grows without "
        "limit under a large or concurrent discovery request"
    )
    assert counter.peak > 1, (
        "no overlap observed at all -- the gate serialised every fetch"
    )


# How long the loop is observed while a REAL git ls-remote sits blocked on a
# never-answering remote. Bounded and short by design (Messi Rule #14): the
# production per-remote timeout is 30 s, and a starved loop is unmistakable long
# before then -- the pre-AC3 code could not serve a single probe in this window.
_BLOCKED_REMOTE_OBSERVATION_SECONDS = 1.5

# How often the concurrent probe fires during that window.
_CONTINUOUS_PROBE_INTERVAL_SECONDS = 0.1

# A healthy loop serves roughly window/interval probes; half of that is the floor
# below which it was demonstrably starved.
_MIN_EXPECTED_PROBES_IN_WINDOW = int(
    _BLOCKED_REMOTE_OBSERVATION_SECONDS / _CONTINUOUS_PROBE_INTERVAL_SECONDS / 2
)

# Listen backlog for the never-answering remote: connections must be accepted by
# the kernel (so git waits for data) but never answered by userspace.
_NEVER_ANSWERING_LISTEN_BACKLOG = 8


@contextlib.contextmanager
def _never_answering_remote() -> Iterator[str]:
    """Yield a git clone URL for a REAL socket that never answers.

    The kernel completes the TCP handshake from the listen backlog while
    userspace never accepts or writes, so a real ``git ls-remote`` against this
    URL blocks waiting for the ref advertisement until it is killed -- a genuine
    unreachable remote, with no sleep and no stubbed subprocess anywhere.

    Loopback with an ephemeral port is intrinsic to the fixture rather than
    environment configuration: the point is a socket this process owns and
    deliberately never answers, which no external host could provide reliably.
    """
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(_NEVER_ANSWERING_LISTEN_BACKLOG)
        port = listener.getsockname()[1]
        yield f"git://127.0.0.1:{port}/story1491-unreachable.git"
    finally:
        listener.close()


def _max_starvation_gap(fire_times: List[float]) -> float:
    """Worst extra delay between consecutive probes, net of their sleep interval.

    This -- not each probe's own round-trip -- is what detects event-loop
    starvation, and the difference was verified empirically: with the discovery
    route's blocking call put back ON the loop, per-probe round-trips still
    measured under 2 ms, because a starved probe is never SCHEDULED and then
    completes instantly once the loop frees. Its absence from the schedule shows
    up only as a gap between fire times. Same reasoning as the barrier-anchored
    measurements elsewhere in this file, which time from the blocking section's
    start rather than from a resumed sleep. Clamped at zero: a healthy loop can
    fire marginally early relative to the nominal interval.
    """
    gaps = [
        (later - earlier) - _CONTINUOUS_PROBE_INTERVAL_SECONDS
        for earlier, later in zip(fire_times, fire_times[1:])
    ]
    return max(0.0, max(gaps, default=0.0))


@pytest.mark.asyncio
async def test_ac3_real_unreachable_remote_route_keeps_loop_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL route + a REAL unreachable remote must not stall the loop.

    Review item 5: AC3's timeout coverage called RemoteBranchService directly,
    bypassing both the route and any concurrency, while the route-level
    concurrency test used a time.sleep stand-in for the blocking work. This
    closes both gaps with NOTHING stood in for that work and the PRODUCTION
    timeout untouched: a real never-answering remote, the real
    RemoteBranchService, and a real ``git ls-remote`` subprocess genuinely
    blocked on it while a second request is issued repeatedly.

    The request is observed for a bounded window and then cancelled rather than
    waited out, because the production per-remote timeout is 30 s and a
    half-minute idle test has no place in this suite. That costs nothing in
    coverage: what must be proven here is that the blocked subprocess does not
    starve the loop, which is fully visible during the window, while the timeout
    ERROR semantics are separately proven end-to-end against the real service by
    test_ac3_unreachable_remote_error_semantics_preserved.

    Before AC3 this route awaited that subprocess ON the event loop, so /ping
    could not be served at all -- visible as a starvation gap in the probe
    schedule (see _max_starvation_gap for why round-trips cannot see it).
    """
    fire_times: List[float] = []

    with _never_answering_remote() as clone_url:
        app = _branch_discovery_app(monkeypatch)
        payload = {"repos": [{"clone_url": clone_url, "platform": "github"}]}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL
        ) as client:
            discovery = asyncio.ensure_future(
                client.post("/api/discovery/branches", json=payload)
            )
            try:
                observation_end = (
                    time.perf_counter() + _BLOCKED_REMOTE_OBSERVATION_SECONDS
                )
                while time.perf_counter() < observation_end:
                    fire_times.append(time.perf_counter())
                    resp = await client.get("/ping")
                    assert resp.status_code == 200
                    await asyncio.sleep(_CONTINUOUS_PROBE_INTERVAL_SECONDS)
                # Recorded, not asserted here: a STARVED loop also makes this
                # true (the request runs to completion before the probe loop is
                # ever rescheduled), and the measured gap below is the signal
                # that must be reported in that case.
                finished_within_window = discovery.done()
            finally:
                discovery.cancel()
                # Await the cancellation so no task is left pending; the real
                # subprocess exits on its own once the socket below closes.
                with contextlib.suppress(asyncio.CancelledError):
                    await discovery

    # PRIMARY starvation signal: how many probes the loop actually served during
    # the window. A frozen loop cannot serve them at all -- measured against a
    # deliberately re-blocked route it served exactly ONE, then the window had
    # already expired by the time the loop resumed.
    minimum_probes = max(2, _MIN_EXPECTED_PROBES_IN_WINDOW)
    assert len(fire_times) >= minimum_probes, (
        f"the event loop served only {len(fire_times)} probe(s) in a "
        f"{_BLOCKED_REMOTE_OBSERVATION_SECONDS}s window (expected at least "
        f"{minimum_probes}) -- it was starved by the blocking git subprocess"
    )
    worst_gap = _max_starvation_gap(fire_times)
    _record(
        "ac3_real_blocked_remote_loop_starvation",
        {
            "observation_window_s": _BLOCKED_REMOTE_OBSERVATION_SECONDS,
            "probe_count": len(fire_times),
            "max_starvation_gap_s": worst_gap,
            "probe_budget_s": _PROMPT_LATENCY_BUDGET_SECONDS,
        },
    )
    assert worst_gap < _PROMPT_LATENCY_BUDGET_SECONDS, (
        "the event loop was starved while a real git ls-remote blocked on an "
        f"unreachable remote (worst gap {worst_gap:.3f}s across "
        f"{len(fire_times)} probes)"
    )
    # Only meaningful once the gap above is healthy: it proves the probes were
    # taken while a real subprocess was genuinely still blocked, so the promptness
    # result cannot be a vacuous pass over an instantly-failed remote.
    assert not finished_within_window, (
        "the discovery request completed inside the observation window, so no "
        "real git subprocess was blocked while probing"
    )


class _FailingTokenStore:
    """Credential store that raises after N successful reads.

    Models the real failure AC3's planning loop must survive: the credential
    store is a genuine external read that can fail partway through a repo list.
    """

    def __init__(self, fail_after: int) -> None:
        self._fail_after = fail_after
        self.calls = 0

    def get_token(self, platform: str) -> None:
        self.calls += 1
        if self.calls > self._fail_after:
            raise RuntimeError("credential store unavailable")
        return None


@pytest.mark.asyncio
async def test_ac3_credential_failure_orphans_no_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-store failure mid-plan must orphan ZERO fetch tasks.

    The planning loop resolves per-repo credentials by really reading the
    credential store, which can raise. If fetch tasks were created while that
    loop was still running, a failure on repo N would leave repos 0..N-1's
    tasks running and never awaited -- unretrieved exceptions plus git
    subprocesses outliving the response. Asserted by counting how many fetches
    ever started: it must be exactly zero.
    """
    from code_indexer.server.services import remote_branch_service as rbs_module
    from code_indexer.server.web import routes as routes_module

    started = 0

    def _counting_fetch(
        service_self: rbs_module.RemoteBranchService,
        clone_url: str,
        platform: str = "github",
        credentials: Optional[str] = None,
    ) -> rbs_module.BranchFetchResult:
        nonlocal started
        started += 1
        return rbs_module.BranchFetchResult(
            success=True, branches=["main"], default_branch="main", error=None
        )

    monkeypatch.setattr(
        rbs_module.RemoteBranchService, "fetch_remote_branches", _counting_fetch
    )
    monkeypatch.setattr(
        routes_module, "_require_admin_session", lambda request: {"username": "admin"}
    )
    # Credentials are resolved ONCE per platform before the planning loop, so
    # the very first read is the one that must fail for this test to exercise
    # the "planning raised" path at all.
    failing_store = _FailingTokenStore(fail_after=0)
    monkeypatch.setattr(routes_module, "_get_token_manager", lambda: failing_store)

    app = _app_with_probe()
    app.post("/api/discovery/branches")(routes_module.fetch_discovery_branches)

    payload = {
        "repos": [
            {
                "clone_url": _UNRESOLVABLE_CLONE_URL_TEMPLATE.format(index=i),
                "platform": "github",
            }
            for i in range(5)
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL
    ) as client:
        resp = await client.post("/api/discovery/branches", json=payload)

    # The route's own outer handler turns the failure into a 500, unchanged.
    assert resp.status_code == 500
    assert started == 0, (
        f"{started} branch fetches were started before planning failed -- those "
        "tasks are orphaned (never awaited, exceptions never retrieved)"
    )


# ===========================================================================
# AC4 -- diagnostics background task (routers/diagnostics.py::run_all_diagnostics)
# ===========================================================================

# A Starlette background task outlives the response, so the test must wait for
# it. Bounded per Messi Rule #14; generous relative to the single _SLOW_SECONDS
# block the stand-in performs.
_BACKGROUND_TASK_DRAIN_TIMEOUT_SECONDS = 60.0
_BACKGROUND_TASK_POLL_SECONDS = 0.02

# Deterministic stdout for the stood-in external process, so the real
# check_cli_tool version-parsing path produces a stable, comparable record.
_CLI_TOOL_STDOUT = b"cidx-diagnostics-stub 1.2.3\n"


class _StubProcess:
    """Stand-in for an asyncio subprocess: the external process boundary."""

    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self) -> Tuple[bytes, bytes]:
        return _CLI_TOOL_STDOUT, b""


def _install_blocking_subprocess_boundary(
    monkeypatch: pytest.MonkeyPatch, barrier: _BlockingBarrier
) -> None:
    """Stand in for asyncio.create_subprocess_exec with a real blocking call.

    Only the EXTERNAL process boundary is replaced -- every real diagnostic
    category method (check_cli_tool's version parsing, error mapping, the
    asyncio.gather fan-out, the cache/DB writes) executes for real.  The FIRST
    invocation after each install performs a REAL synchronous ``time.sleep``
    inside the coroutine, which is precisely the shape report Finding B4
    describes; later invocations return immediately so the run stays fast.

    Must be re-installed before every diagnostics run that needs to block --
    the one-shot latch is per-install, not per-process.
    """
    blocked_once = threading.Event()

    async def _stub_exec(*args: object, **kwargs: object) -> _StubProcess:
        if not blocked_once.is_set():
            blocked_once.set()
            # Anchor the probes to the exact start of the real block rather than
            # to a guessed stagger (review item 16).
            barrier.enter()
            time.sleep(_SLOW_SECONDS)
        return _StubProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _stub_exec)


def _new_diagnostics_service(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> "DiagnosticsService":
    """A real DiagnosticsService on a temp DB, with no external credentials.

    The credential store is the other external boundary: with no stored
    platform tokens every API check takes its real NOT_CONFIGURED branch, so
    the test never performs network IO.
    """
    from code_indexer.server.services.diagnostics_service import DiagnosticsService

    service = DiagnosticsService(db_path=str(db_path))
    monkeypatch.setattr(service, "_get_token_manager", _NoStoredTokens)
    return service


def _comparable_records(
    service: "DiagnosticsService",
) -> Dict[str, List[Dict[str, object]]]:
    """Persisted diagnostic records, minus the inherently volatile timestamp."""
    comparable: Dict[str, List[Dict[str, object]]] = {}
    for category, results in service.get_status().items():
        comparable[category.value] = [
            {k: v for k, v in result.to_dict().items() if k != "timestamp"}
            for result in results
        ]
    return comparable


async def _await_diagnostics_completion(service: "DiagnosticsService") -> None:
    """Bounded wait for the in-flight diagnostics background task to finish."""
    deadline = time.monotonic() + _BACKGROUND_TASK_DRAIN_TIMEOUT_SECONDS
    while service.is_running() and time.monotonic() < deadline:
        await asyncio.sleep(_BACKGROUND_TASK_POLL_SECONDS)
    assert not service.is_running(), "diagnostics background task never completed"


@pytest.mark.asyncio
async def test_ac4_diagnostics_background_task_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A diagnostics run must not freeze the loop for other requests.

    Report Finding B4: ``run_all_diagnostics`` was registered as an ``async``
    Starlette BackgroundTask, and Starlette awaits async background tasks ON
    the loop -- its synchronous SQLite writes and per-repo/per-collection NFS
    ``exists``/``open``/``json.load`` calls therefore blocked every other
    connection.  A sync background-task entry is threadpooled by Starlette
    instead.

    Two properties are asserted:

    1. A concurrent /ping is served promptly while the run is in flight.
    2. The persisted records are IDENTICAL to a baseline produced by running
       the pre-change execution path (the async ``run_all_diagnostics``
       coroutine, still present) directly on an identically-configured
       service -- an exact record-for-record comparison, not a smoke check.
    """
    from code_indexer.server.routers import diagnostics as diagnostics_router

    # Baseline: the pre-change path, awaited directly on the event loop. Its
    # barrier is never observed -- no concurrent probe runs against the
    # baseline here, only the records it produces are compared.
    _install_blocking_subprocess_boundary(monkeypatch, _BlockingBarrier())
    baseline_service = _new_diagnostics_service(monkeypatch, tmp_path / "baseline.db")
    await baseline_service.run_all_diagnostics()
    baseline_records = _comparable_records(baseline_service)

    # Under test: the real route + real background-task registration.  The
    # blocking boundary is re-installed so this run blocks too (the latch above
    # was consumed by the baseline run), this time with the barrier the probes
    # are anchored to.
    barrier = _BlockingBarrier()
    _install_blocking_subprocess_boundary(monkeypatch, barrier)
    service = _new_diagnostics_service(monkeypatch, tmp_path / "under_test.db")
    monkeypatch.setattr(diagnostics_router, "diagnostics_service", service)
    app = _app_with_probe()
    app.post("/run-all")(diagnostics_router.run_all_diagnostics)

    async def _slow(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/run-all")

    measured = await _measure_probe_latency_during(app, _slow, barrier=barrier)
    assert _slow_response(measured).status_code == 200

    _record(
        "ac4_diagnostics_background_task",
        {
            "background_block_s": _SLOW_SECONDS,
            "measured_probe_latencies_s": _probe_latencies(measured),
            "measured_max_probe_latency_s": _max_probe_latency(measured),
            "probe_budget_s": _PROMPT_LATENCY_BUDGET_SECONDS,
        },
    )
    _assert_probe_was_prompt(measured, "the diagnostics background task")

    await _await_diagnostics_completion(service)
    assert _comparable_records(service) == baseline_records


def test_ac4_diagnostics_get_status_routes_are_sync_dispatched() -> None:
    """Every diagnostics route calling get_status() must be a plain def.

    ``get_status()`` performs a blocking SQLite read for any cold/expired
    category. FastAPI runs a plain ``def`` route in its threadpool, so
    declaring these routes sync IS what keeps that read off the event loop.
    If one is ever re-declared ``async def``, the exact defect class this
    story exists to close silently returns -- in the file this story rewrote.
    """
    from code_indexer.server.routers import diagnostics as diagnostics_router

    for route_name in (
        "get_diagnostics_page",
        "get_diagnostics_status",
        "run_all_diagnostics",
        "run_category_diagnostics",
    ):
        handler = getattr(diagnostics_router, route_name)
        assert not asyncio.iscoroutinefunction(handler), (
            f"{route_name} calls get_status() (blocking SQLite on a cold "
            "category) and must be a sync def route so FastAPI threadpools it"
        )


# Concurrency used to hammer the lock property from many threads at once.
_LOCK_RACE_THREADS = 32
_LOCK_RACE_BARRIER_TIMEOUT_S = 10.0


def test_ac4_lock_property_is_not_lazily_constructed(tmp_path: Path) -> None:
    """Every caller must receive the SAME lock object, always.

    A lazily-constructed lock (``if self.__lock is None: self.__lock =
    threading.Lock()``) is itself a race: two concurrent FIRST callers can both
    observe None and each construct their OWN Lock. Each then believes it holds
    "the" lock while actually holding a different one -- silently defeating
    every bit of synchronisation this story added.

    Asserted structurally (the lock must already exist before any access) AND
    by hammering the property from many threads and requiring exactly one
    distinct object. Worker completion is verified explicitly so the
    single-distinct-object assertion can never pass vacuously.
    """
    from code_indexer.server.services.diagnostics_service import DiagnosticsService

    service = DiagnosticsService(db_path=str(tmp_path / "lock.db"))

    # Eagerly constructed: a fresh instance must already own its lock, so the
    # first-caller race window cannot exist at all.
    assert service._DiagnosticsService__lock is not None, (  # type: ignore[attr-defined]
        "the lock must be constructed eagerly in __init__, not lazily on "
        "first access -- lazy construction lets two concurrent first callers "
        "each build a separate Lock"
    )

    seen: List[int] = []
    failures: List[BaseException] = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(_LOCK_RACE_THREADS)

    def _grab() -> None:
        try:
            barrier.wait(timeout=_LOCK_RACE_BARRIER_TIMEOUT_S)
            obtained = service._lock
            with seen_lock:
                seen.append(id(obtained))
        except BaseException as exc:  # recorded, never swallowed
            with seen_lock:
                failures.append(exc)

    threads = [threading.Thread(target=_grab) for _ in range(_LOCK_RACE_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_LOCK_RACE_BARRIER_TIMEOUT_S)

    assert not failures, f"worker threads raised: {failures!r}"
    assert all(not t.is_alive() for t in threads), "a worker thread never finished"
    assert len(seen) == _LOCK_RACE_THREADS, (
        f"only {len(seen)} of {_LOCK_RACE_THREADS} workers recorded a lock -- "
        "the distinctness assertion below would be vacuous"
    )
    assert len(set(seen)) == 1, (
        f"{len(set(seen))} distinct lock objects were handed out to "
        f"{_LOCK_RACE_THREADS} concurrent callers -- they are not mutually "
        "excluding each other at all"
    )


# Marker messages distinguishing the two competing writes in the lost-update
# race test below.
_STALE_DB_MESSAGE = "stale-from-database"
_NEWER_RUN_MESSAGE = "newer-from-concurrent-run"

# How far in the past the simulated DB snapshot is stamped. Any duration
# comfortably older than the competing write works; one hour is unambiguous.
_STALE_SNAPSHOT_AGE = timedelta(hours=1)


def _diagnostic_result_with(message: str) -> List["DiagnosticResult"]:
    """One WORKING CLI-tools result carrying the given marker message."""
    from code_indexer.server.services.diagnostics_service import (
        DiagnosticResult,
        DiagnosticStatus,
    )

    return [
        DiagnosticResult(
            name="cli-tools",
            status=DiagnosticStatus.WORKING,
            message=message,
            details={},
        )
    ]


def _make_racing_diagnostics_service(
    db_path: Path, raced_category: "DiagnosticCategory"
) -> "DiagnosticsService":
    """A real DiagnosticsService that lands the lost-update race exactly.

    Its ``_read_category_from_db`` publishes a NEWER generation through the
    REAL lock -- byte-for-byte the sequence a concurrent background run
    performs -- and only then returns its own OLDER snapshot. This makes the
    race deterministic: no sleeps, no thread scheduling assumptions.

    The publish deliberately goes through the same private cache dicts the
    production write paths use, because that IS the shared state under test;
    there is no public setter for "a background run just published".
    """
    from code_indexer.server.services.diagnostics_service import DiagnosticsService

    class _RacingService(DiagnosticsService):
        def _read_category_from_db(self, category):  # type: ignore[no-untyped-def]
            if category is not raced_category:
                return None
            with self._lock:
                self._cache[category] = _diagnostic_result_with(_NEWER_RUN_MESSAGE)
                self._cache_timestamps[category] = datetime.now()
                # Bump the generation exactly as the real publish sites
                # (run_all_diagnostics / run_category) now do -- the CAS token
                # is the monotonic generation, not the timestamp, so a
                # simulation that skipped this would model a publish no
                # production path performs.
                self._cache_generation[category] = next(self._generation_counter)
            stale_at = datetime.now() - _STALE_SNAPSHOT_AGE
            return _diagnostic_result_with(_STALE_DB_MESSAGE), stale_at

    return _RacingService(db_path=str(db_path))


def test_ac4_get_status_publish_back_does_not_clobber_newer_results(
    tmp_path: Path,
) -> None:
    """get_status()'s publish-back must not overwrite a NEWER concurrent write.

    ``get_status()`` correctly performs its DB read with the lock RELEASED, but
    then publishes the result back unconditionally. Meanwhile
    ``run_all_diagnostics`` is now a SYNC Starlette background task on a
    threadpool worker -- a concurrency this very story introduced -- so it can
    publish genuinely fresher results for the same category DURING that read
    window. An unconditional publish-back silently discards them: a classic
    lost update, in which the freshest data loses.
    """
    from code_indexer.server.services.diagnostics_service import DiagnosticCategory

    raced_category = DiagnosticCategory.CLI_TOOLS
    service = _make_racing_diagnostics_service(tmp_path / "race.db", raced_category)

    status = service.get_status()

    published = [r.message for r in service.get_category_status(raced_category)]
    assert _NEWER_RUN_MESSAGE in published, (
        "get_status()'s publish-back clobbered a NEWER concurrent write with "
        f"its stale DB read (cache now holds {published})"
    )
    returned = [r.message for r in status[raced_category]]
    assert _NEWER_RUN_MESSAGE in returned, (
        "get_status() returned the stale DB read instead of the newer "
        f"concurrent results (returned {returned})"
    )


# ===========================================================================
# AC2 -- regex_search MCP dispatch (mcp/handlers/search.py::handle_regex_search)
# ===========================================================================

# Corpus size: large enough that the handler's SYNCHRONOUS sections take
# hundreds of milliseconds of REAL work -- no artificial sleep is used anywhere
# in the AC2 proof. Deliberately just under _MAX_PREFILTER_CANDIDATES (8000):
# above that ceiling regex_search SKIPS the trigram pre-filter entirely, and the
# pre-filter plus its Path.resolve fan-out are two of the heaviest synchronous
# line items AC2 names.
_REGEX_CORPUS_FILES = 7500
_REGEX_CORPUS_PACKAGES = 50
_REGEX_REPO_ALIAS = "story1491repo"
_REGEX_PATTERN = "needle_target_"

# Lower bound on pre-filter candidates the measured runs must actually have
# resolved. Proves the corpus really exercises the trigram intersection and the
# per-candidate Path.resolve fan-out rather than silently full-scanning.
_MIN_OBSERVED_PREFILTER_CANDIDATES = 1000


def _build_regex_corpus(root: Path) -> None:
    """Create a real on-disk corpus that ripgrep will really search.

    Includes a real ``.git`` directory so the production resolver
    (``_legacy._resolve_repo_path``) accepts the absolute path via its
    full-path branch -- no server-only registry wiring needed, and the whole
    resolve + search + parse path stays real.

    Also builds a REAL trigram index with the production TrigramIndexManager,
    which is what makes ``_prefilter_candidate_files`` do its actual work: a
    multi-statement SQLite trigram intersection followed by one
    ``Path.resolve()`` per candidate (report Finding B2's first two line items).
    Without an index present that method returns None immediately and neither
    line item is ever measured.
    """
    from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager

    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    for index in range(_REGEX_CORPUS_FILES):
        package = root / f"pkg{index % _REGEX_CORPUS_PACKAGES}"
        package.mkdir(parents=True, exist_ok=True)
        (package / f"mod{index}.py").write_text(
            f"def {_REGEX_PATTERN}{index}():\n    return {index}\n"
        )
    TrigramIndexManager(root / ".code-indexer" / "trigram_index").build(root)


class _PrefilterObserver:
    """Pass-through observer around the real trigram pre-filter.

    Signals the CURRENT measurement's barrier at the instant the pre-filter
    starts -- the handler's first synchronous section, and the one report
    Finding B2 lists first -- then calls the real implementation unchanged and
    records how many candidates it resolved.  Nothing about the pre-filter's
    behaviour is altered; this only observes WHEN it runs and WHAT it produced,
    which is what lets the test prove the corpus really exercises the SQLite
    trigram intersection and its per-candidate ``Path.resolve()`` fan-out.
    """

    def __init__(self) -> None:
        self.barrier: Optional[_BlockingBarrier] = None
        self._lock = threading.Lock()
        self._candidate_counts: List[int] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from code_indexer.global_repos import regex_search as regex_search_module

        real = regex_search_module.RegexSearchService._prefilter_candidate_files

        def _observed(
            service_self: object,
            pattern: str,
            search_path: Path,
            path: Optional[str],
            case_sensitive: bool,
        ) -> Optional[List[Path]]:
            if self.barrier is not None:
                self.barrier.enter()
            raw = real(service_self, pattern, search_path, path, case_sensitive)
            result: Optional[List[Path]] = None if raw is None else list(raw)
            with self._lock:
                # -1 records "no usable pre-filter, full scan" distinctly from a
                # real but empty candidate list.
                self._candidate_counts.append(-1 if result is None else len(result))
            return result

        monkeypatch.setattr(
            regex_search_module.RegexSearchService,
            "_prefilter_candidate_files",
            _observed,
        )

    def max_candidates(self) -> int:
        with self._lock:
            return max(self._candidate_counts) if self._candidate_counts else 0


def _regex_dispatch_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Tuple[FastAPI, _PrefilterObserver]:
    """Real app with two routes, both dispatching via the REAL MCP dispatcher.

    ``/dispatch/registered`` invokes whatever ``regex_search`` handler the
    production registry exposes (the thing this story changes).
    ``/dispatch/async-impl`` invokes the async implementation the same way the
    dispatcher did BEFORE the change, giving a real pre-change measurement to
    compare against.  Nothing about the dispatcher, the handler, ripgrep, or
    the filesystem is mocked: only ``app.state.golden_repos_dir`` is pointed at
    the temp corpus so repo resolution finds it.

    Returns the app plus the pre-filter observer, whose ``barrier`` the caller
    sets before each measurement.
    """
    import inspect

    import code_indexer.server.app as app_module
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.mcp import protocol as protocol_module
    from code_indexer.server.mcp.handlers import search as search_handlers

    golden_repos_dir = tmp_path / "golden-repos"
    _build_regex_corpus(golden_repos_dir / _REGEX_REPO_ALIAS)
    monkeypatch.setattr(
        app_module.app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
    )

    observer = _PrefilterObserver()
    observer.install(monkeypatch)

    user = User(
        username="story1491",
        password_hash=_PLACEHOLDER_PASSWORD_HASH,
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    registry: Dict[str, Callable[..., Any]] = {}
    search_handlers._register(registry)

    async def _dispatch(handler: Callable[..., Any]) -> JSONResponse:
        arguments = {
            # Absolute path: the production resolver's full-path branch, which
            # the corpus satisfies (it is a real git working directory).
            "repository_alias": str(golden_repos_dir / _REGEX_REPO_ALIAS),
            "pattern": _REGEX_PATTERN,
            "max_results": _REGEX_CORPUS_FILES,
        }
        result = await protocol_module._invoke_handler(
            handler,
            arguments,
            user,
            None,
            inspect.signature(handler),
            asyncio.iscoroutinefunction(handler),
            tool_name="regex_search",
        )
        return JSONResponse(content=result)

    app = _app_with_probe()

    @app.post("/dispatch/registered")
    async def _dispatch_registered() -> JSONResponse:
        return await _dispatch(registry["regex_search"])

    @app.post("/dispatch/async-impl")
    async def _dispatch_async_impl() -> JSONResponse:
        return await _dispatch(search_handlers.handle_regex_search)

    return app, observer


def test_ac2_pcre2_probe_runs_at_most_once_per_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `rg --pcre2-version` fork+exec must not run per request (AC2).

    A fresh RegexSearchService is constructed for every regex_search request,
    so a per-instance cache meant this subprocess ran on every pcre2 request.
    Counts real invocations at the OS-subprocess boundary across several
    freshly-constructed services.
    """
    import subprocess

    from code_indexer.global_repos import regex_search as regex_search_module

    monkeypatch.setattr(
        regex_search_module.RegexSearchService, "_pcre2_supported_global", None
    )
    probe_calls = 0

    def _counting_run(*args: object, **kwargs: object) -> _CompletedProcessStub:
        # The only subprocess.run this code path can reach is the pcre2 probe
        # (search-engine detection uses shutil.which), so counting here counts
        # exactly the fork+exec AC2 is about. A stub reply keeps the count
        # deterministic and independent of the local ripgrep build.
        nonlocal probe_calls
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and command[:2] == ["rg", "--pcre2-version"]:
            probe_calls += 1
        return _CompletedProcessStub()

    monkeypatch.setattr(subprocess, "run", _counting_run)

    repo = tmp_path / "repo"
    repo.mkdir()
    for _ in range(4):
        service = regex_search_module.RegexSearchService(repo)
        service._detect_pcre2_support()

    assert probe_calls == 1, (
        f"pcre2 probe forked {probe_calls} times across 4 per-request services "
        "-- it must be cached process-wide"
    )


# How much room the floor must leave below the measured minimum baseline stall
# before run-to-run noise can reach it, and how far above the offloaded path's
# own latency it must sit before the guard could be satisfied vacuously.
_AC2_FLOOR_NOISE_MARGIN_FACTOR = 2.0
_AC2_FLOOR_VACUITY_MARGIN_FACTOR = 10.0


def _constant_assignment_value_node(name: str) -> ast.expr:
    """Return the expression this module assigns to a named constant.

    Structural, not value-based: the point is to see HOW the constant is
    written, which no runtime read of its value can reveal.
    """
    source = Path(__file__).read_text()
    for node in ast.parse(source).body:
        targets: List[ast.expr]
        value: Optional[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                assert value is not None, f"{name} is declared without a value"
                return value
    raise AssertionError(f"{name} is not assigned at module level")


def test_ac2_anti_vacuity_floor_is_decoupled_from_the_promptness_budget() -> None:
    """The floor must track the real stall, not AC2's promptness budget.

    Round 4 flake, reproduced and measured: the floor used to be defined as
    ``4 * _AC2_PROBE_BUDGET_SECONDS``, so raising that budget 0.05 -> 0.1 (a
    change about how fast the FIXED path must answer) silently doubled the floor
    to 0.4s -- landing it inside the noise band of the pre-change baseline,
    whose measured stall is 0.39-0.45s across 12 runs, idle and under load.  One
    run in six came in at 0.3906s and failed the precondition.  The two
    quantities are unrelated: how long the unfixed blocking section takes is a
    property of the corpus and the machine, and does not move when the
    promptness bar moves.

    Pins the decoupling STRUCTURALLY (the floor must be a standalone literal,
    so no expression can make it move with the budget) plus both margins it has
    to respect.
    """
    floor_node = _constant_assignment_value_node("_AC2_MIN_BASELINE_STALL_SECONDS")
    assert isinstance(floor_node, ast.Constant), (
        "the anti-vacuity floor is a derived expression, so changing another "
        "constant (the promptness budget, historically) silently moves it -- "
        "which is exactly what made this test flaky. It must be a literal, "
        "sourced from the measured duration of the real blocking section."
    )
    assert (
        _AC2_MIN_BASELINE_STALL_SECONDS * _AC2_FLOOR_NOISE_MARGIN_FACTOR
        <= _AC2_OBSERVED_MIN_BASELINE_STALL_SECONDS
    ), (
        f"the floor ({_AC2_MIN_BASELINE_STALL_SECONDS}s) leaves under "
        f"{_AC2_FLOOR_NOISE_MARGIN_FACTOR}x margin below the measured minimum "
        f"baseline stall ({_AC2_OBSERVED_MIN_BASELINE_STALL_SECONDS}s) -- "
        "run-to-run noise will trip it"
    )
    assert (
        _AC2_MIN_BASELINE_STALL_SECONDS
        >= _AC2_FLOOR_VACUITY_MARGIN_FACTOR * _AC2_OBSERVED_MAX_OFFLOADED_PROBE_SECONDS
    ), (
        "the floor is close enough to the offloaded path's own latency "
        f"({_AC2_OBSERVED_MAX_OFFLOADED_PROBE_SECONDS}s) that an already-fixed "
        "path could satisfy it -- the guard would be vacuous"
    )
    assert _AC2_MIN_BASELINE_STALL_SECONDS > _AC2_PROBE_BUDGET_SECONDS, (
        "a 'stalling' baseline must at minimum be slower than the bar the fixed "
        "path is required to beat"
    )


def _regex_match_count(resp: httpx.Response) -> int:
    payload = json.loads(resp.json()["content"][0]["text"])
    assert payload["success"] is True, payload
    return int(payload["total_matches"])


@pytest.mark.asyncio
# Headroom, not a hang allowance: this test builds a 7500-file corpus and a real
# trigram index, then runs real ripgrep over it twice. Measured at 6.9s idle and
# 10.2s alongside five concurrent pytest processes -- under 1.5x below
# server-fast-automation.sh's 15s per-test cap, which runs six chunks in
# parallel. That margin is thin enough to lose to ordinary gate load, and a
# timeout kill here would read as a story regression rather than as scheduling.
@pytest.mark.timeout(60)
async def test_ac2_regex_search_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """regex_search's synchronous work must leave the event loop.

    Report Finding B2: ``handle_regex_search`` was async-dispatched, so its
    SQLite trigram prefilter, up to 8000 ``Path.resolve()`` calls, the
    whole multi-MB ripgrep output read and the per-line ``json.loads`` all ran
    directly on the event loop, with no timeout anywhere on that path.  Making
    the registered handler a plain ``def`` hands it to the dispatcher's
    executor branch (``protocol.py``'s ``run_in_executor``), moving all of it
    off the loop.

    Measured with REAL work over a REAL corpus and REAL ripgrep -- no sleeps.
    Both dispatch paths are exercised through the REAL ``_invoke_handler``, and
    their results must be identical.

    Review item 2: the comparison is only trusted AFTER a precondition proves
    the pre-change baseline genuinely stalls the loop.  The previous revision
    asserted a single absolute budget that the OLD, unfixed async dispatch
    already satisfied, so it could pass without proving anything.  Two things
    fix that: probes anchored to the barrier the pre-filter enters (so timing no
    longer depends on a stagger guess), and a corpus carrying a REAL trigram
    index so the pre-filter and its ``Path.resolve()`` fan-out -- AC2's two
    heaviest synchronous line items -- actually execute.
    """
    app, observer = _regex_dispatch_app(monkeypatch, tmp_path)

    async def _slow_async_impl(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/dispatch/async-impl")

    async def _slow_registered(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/dispatch/registered")

    before_barrier = _BlockingBarrier()
    observer.barrier = before_barrier
    before = await _measure_probe_latency_during(
        app, _slow_async_impl, barrier=before_barrier
    )

    after_barrier = _BlockingBarrier()
    observer.barrier = after_barrier
    after = await _measure_probe_latency_during(
        app, _slow_registered, barrier=after_barrier
    )

    before_resp = _slow_response(before)
    after_resp = _slow_response(after)
    assert before_resp.status_code == 200
    assert after_resp.status_code == 200
    # Identical results: this story changes WHERE the work runs, not what it
    # returns.
    assert _regex_match_count(after_resp) == _regex_match_count(before_resp)
    assert _regex_match_count(after_resp) > 0

    before_probe = _max_probe_latency(before)
    after_probe = _max_probe_latency(after)
    observed_candidates = observer.max_candidates()
    _record(
        "ac2_regex_search_dispatch",
        {
            "corpus_files": _REGEX_CORPUS_FILES,
            "matches": _regex_match_count(after_resp),
            "observed_prefilter_candidates": observed_candidates,
            "before_async_dispatch_total_wall_s": _total_wall(before),
            "before_async_dispatch_probe_latencies_s": _probe_latencies(before),
            "before_async_dispatch_max_probe_latency_s": before_probe,
            "after_sync_dispatch_total_wall_s": _total_wall(after),
            "after_sync_dispatch_probe_latencies_s": _probe_latencies(after),
            "after_sync_dispatch_max_probe_latency_s": after_probe,
            "ac2_probe_budget_s": _AC2_PROBE_BUDGET_SECONDS,
            "ac2_min_baseline_stall_s": _AC2_MIN_BASELINE_STALL_SECONDS,
        },
    )

    # The corpus must really drive the trigram pre-filter and its per-candidate
    # resolve fan-out. A full-scan fallback records -1 and fails here.
    assert observed_candidates >= _MIN_OBSERVED_PREFILTER_CANDIDATES, (
        "the SQLite trigram pre-filter and its Path.resolve fan-out were not "
        f"exercised (max observed candidates {observed_candidates}); AC2's two "
        "heaviest synchronous line items would go unmeasured"
    )

    # PRECONDITION (review item 2): the baseline must be a genuine event-loop
    # stall before its comparison against the fixed path means anything.
    minimum_baseline_stall = _AC2_MIN_BASELINE_STALL_SECONDS
    assert before_probe >= minimum_baseline_stall, (
        "NON-DISCRIMINATING MEASUREMENT: the pre-change async-dispatch baseline "
        f"only stalled the loop for {before_probe:.4f}s, under the "
        f"{minimum_baseline_stall:.4f}s this test requires before trusting any "
        "before/after comparison. The corpus is too small (or the pre-filter did "
        "not run), so a passing 'after' would prove nothing about the fix."
    )

    assert after_probe < _AC2_PROBE_BUDGET_SECONDS, (
        "a concurrent request was delayed while regex_search ran -- the "
        f"blocking work is still on the event loop (max probe latency "
        f"{after_probe:.4f}s, budget {_AC2_PROBE_BUDGET_SECONDS}s)"
    )
    assert after_probe * _MIN_PROBE_IMPROVEMENT_RATIO < before_probe, (
        "sync dispatch did not measurably free the event loop: probe latency "
        f"{after_probe:.4f}s vs pre-change async dispatch {before_probe:.4f}s"
    )

    # Deterministic dispatch property, independent of any timing: the REGISTERED
    # handler is not a coroutine function, so protocol.py provably takes its
    # run_in_executor branch (whose off-loop thread execution is itself pinned by
    # tests/unit/server/mcp/test_invoke_handler_executor.py).
    from code_indexer.server.mcp.handlers import search as search_handlers

    registered_handler: Dict[str, Callable[..., Any]] = {}
    search_handlers._register(registered_handler)
    assert not asyncio.iscoroutinefunction(registered_handler["regex_search"]), (
        "regex_search must be sync-dispatched so the protocol dispatcher "
        "offloads its synchronous work to the executor (Story #1491 AC2)"
    )


# A small corpus is deliberate here: this test proves RESULT EQUIVALENCE across
# the two dispatch paths, not timing, and two repos are enough to exercise the
# omni fan-out (which loops repos sequentially).
_OMNI_CORPUS_FILES = 20
_OMNI_REPO_ALIASES = ("story1491omnia", "story1491omnib")

# The ONLY field excluded from the payload comparison: wall-clock search time,
# which necessarily differs between two runs of the same real search.
_VOLATILE_PAYLOAD_KEY = "search_time_ms"

# The match LIST is compared as an order-insensitive collection, and that is a
# property of the production code, not a convenience. Measured on ONE unchanged
# dispatch path, four identical searches over the same 20-file corpus returned
# four DIFFERENT match orders (e.g. run 0 started mod19, mod9, mod18, mod14
# while run 1 started mod19, mod18, mod17, mod16): ripgrep walks files on
# multiple threads and regex_search does not pass --sort, so output order is
# nondeterministic per run. Asserting order equality here would therefore assert
# a guarantee regex_search never made, and would fail at random against
# completely correct code. Every match and every other field is still compared
# exactly.
_MATCH_SORT_FIELDS = ("source_repo", "file_path", "line_number", "column")


def _strip_volatile(value: object) -> object:
    """Recursively drop the wall-clock timing so payloads are comparable."""
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key != _VOLATILE_PAYLOAD_KEY
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _comparable_regex_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Payload with timings dropped and matches put in a deterministic order."""
    comparable = _strip_volatile(payload)
    assert isinstance(comparable, dict)
    matches = comparable.get("matches")
    if isinstance(matches, list):
        comparable["matches"] = sorted(
            matches,
            key=lambda match: tuple(
                str(match.get(field, "")) for field in _MATCH_SORT_FIELDS
            ),
        )
    return comparable


def _omni_regex_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Publish two repos as global aliases and enable wildcard expansion.

    Everything the omni path touches is real: two real on-disk working trees
    (each carrying a ``.git`` marker directory, which is what the production
    resolver's ``_is_git_repo`` check looks for -- regex search reads the working
    tree, so no commits are involved), a real ``AliasManager`` pointer file per
    repo (the resolver's first-priority lookup), real ripgrep, and the real
    response formatting.  The only stand-ins are the two registry lookups a live
    server would answer from its database: the global-repo listing that wildcard
    expansion enumerates, and the access-filtering service (absent, i.e. no
    restriction).
    """
    import code_indexer.server.app as app_module
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.server.mcp.handlers import _utils

    golden_repos_dir = tmp_path / "golden-repos"
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True, exist_ok=True)
    alias_manager = AliasManager(str(aliases_dir))

    for ordinal, alias in enumerate(_OMNI_REPO_ALIASES):
        repo_root = golden_repos_dir / alias
        (repo_root / ".git").mkdir(parents=True, exist_ok=True)
        (repo_root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        for index in range(_OMNI_CORPUS_FILES):
            (repo_root / f"mod{index}.py").write_text(
                f"def {_REGEX_PATTERN}{ordinal}_{index}():\n    return {index}\n"
            )
        alias_manager.create_alias(f"{alias}-global", str(repo_root), repo_name=alias)

    monkeypatch.setattr(
        app_module.app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
    )
    monkeypatch.setattr(
        _utils,
        "_list_global_repos",
        lambda: [{"alias_name": f"{alias}-global"} for alias in _OMNI_REPO_ALIASES],
    )
    monkeypatch.setattr(_utils, "_get_access_filtering_service", lambda: None)


@pytest.mark.asyncio
async def test_ac2_omni_regex_search_payload_identical_across_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An omni (*) regex search must return the SAME payload after AC2.

    AC2 requires it explicitly: "an omni (*) regex search over multiple repos
    still returns the same results as before".  The omni path is the amplifier
    Finding B2 names -- ``_omni_regex_search`` loops repos SEQUENTIALLY, calling
    the async ``handle_regex_search`` once per repo -- and switching the
    REGISTERED handler to a sync wrapper that drives that coroutine on a private
    loop is exactly the kind of change that could reorder or drop results.

    Compares the WHOLE decoded payload (every match with all of its fields,
    total_matches, truncated, search_engine, repos_searched, errors and any
    query_metadata) between the registered handler and the original coroutine,
    excluding only the wall-clock search time.  Matches are compared as an
    order-insensitive collection because ripgrep's own output order is
    nondeterministic per run -- see the _MATCH_SORT_FIELDS comment for the
    measurement that established this.  Both go through the REAL dispatcher.
    """
    import inspect

    from code_indexer.server.mcp import protocol as protocol_module
    from code_indexer.server.mcp.handlers import search as search_handlers

    from code_indexer.server.auth.user_manager import User, UserRole

    _omni_regex_env(monkeypatch, tmp_path)
    user = User(
        username="story1491",
        password_hash=_PLACEHOLDER_PASSWORD_HASH,
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    registry: Dict[str, Callable[..., Any]] = {}
    search_handlers._register(registry)

    async def _dispatch(handler: Callable[..., Any]) -> Dict[str, Any]:
        # A fresh argument dict per dispatch: handle_regex_search normalises
        # repository_alias in place.
        arguments: Dict[str, Any] = {
            "repository_alias": "*",
            "pattern": _REGEX_PATTERN,
            "max_results": _OMNI_CORPUS_FILES * len(_OMNI_REPO_ALIASES),
        }
        result = await protocol_module._invoke_handler(
            handler,
            arguments,
            user,
            None,
            inspect.signature(handler),
            asyncio.iscoroutinefunction(handler),
            tool_name="regex_search",
        )
        payload = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict)
        return payload

    async_payload = await _dispatch(search_handlers.handle_regex_search)
    sync_payload = await _dispatch(registry["regex_search"])

    assert async_payload.get("success") is True, async_payload
    assert sync_payload.get("success") is True, sync_payload
    # Real matches from BOTH repos, otherwise the comparison is vacuous.
    assert sync_payload["total_matches"] == (
        _OMNI_CORPUS_FILES * len(_OMNI_REPO_ALIASES)
    )
    assert sync_payload["repos_searched"] == len(_OMNI_REPO_ALIASES)
    assert {match["source_repo"] for match in sync_payload["matches"]} == {
        f"{alias}-global" for alias in _OMNI_REPO_ALIASES
    }

    assert _comparable_regex_payload(sync_payload) == _comparable_regex_payload(
        async_payload
    ), (
        "the sync-dispatched omni regex payload differs from the original "
        "coroutine's -- AC2 permits a change in WHERE the work runs, never in "
        "what it returns"
    )


# ===========================================================================
# AC1 -- MCP auth dependency (server/auth/dependencies.py)
# ===========================================================================

# bcrypt cost factor. 12 is deliberately high so ONE verification takes
# 100-300 ms of real, GIL-held CPU -- the exact cost report Finding B1 measured
# on every OAuth/Basic MCP request. No sleep is used in the AC1 proof.
_BCRYPT_ROUNDS = 12


class _RealBcryptCredentialStore:
    """Credential STORE stand-in that performs a REAL bcrypt verification.

    Only the storage of the credential is stood in for (the real one is a DB
    table); ``verify_credential`` runs the genuine ``bcrypt.checkpw`` against a
    genuine bcrypt hash, so the 100-300 ms of GIL-held CPU that report Finding
    B1 is about is really executed on the real dependency's code path.
    """

    def __init__(self, client_id: str, secret: str, user_id: str) -> None:
        import bcrypt

        self._client_id = client_id
        self._user_id = user_id
        self._hash = bcrypt.hashpw(
            secret.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
        )
        # Set per measurement; marks the instant the real bcrypt work starts so
        # probes are anchored to it rather than to a guessed stagger (item 16).
        self.barrier: Optional[_BlockingBarrier] = None

    def verify_credential(self, client_id: str, client_secret: str) -> Optional[str]:
        import bcrypt

        if client_id != self._client_id:
            return None
        if self.barrier is not None:
            self.barrier.enter()
        if not bcrypt.checkpw(client_secret.encode("utf-8"), self._hash):
            return None
        return self._user_id


class _SingleUserManager:
    """Stand-in for the dependency's EXTERNAL user store.

    The system under test is ``get_current_user_for_mcp``. ``user_manager`` is
    not part of it -- it is the dependency's collaborator backed by the user
    database, i.e. exactly the kind of external boundary a unit test may stand
    in for (the same boundary the pre-existing
    test_mcp_auth_off_event_loop_1491.py already stands in for).

    Records the thread identity of every ``get_user`` call so a test can prove
    the synchronous user-DB read is offloaded to a worker thread rather than
    executed on the event loop (Story #1491 AC1's third bullet). The list is
    written from a worker thread and read from the event-loop thread, so it is
    lock-guarded.
    """

    def __init__(self, user_id: str, user: object) -> None:
        self._user_id = user_id
        self._user = user
        self._lock = threading.Lock()
        self._call_thread_idents: List[int] = []

    def get_user(self, user_id: str) -> object:
        with self._lock:
            self._call_thread_idents.append(threading.get_ident())
        return self._user if user_id == self._user_id else None

    def recorded_thread_idents(self) -> List[int]:
        with self._lock:
            return list(self._call_thread_idents)


def _wire_real_bcrypt_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[httpx.BasicAuth, _SingleUserManager]:
    """Wire the REAL auth dependency's external collaborators.

    Every opaque value here is generated fresh per run by ``secrets``, is never
    written anywhere, and is meaningful only to the in-memory stand-in store
    created in this function -- it authenticates against nothing real. Their
    only purpose is to make the real bcrypt comparison perform real work.
    """
    import code_indexer.server.auth.dependencies as deps_module
    from code_indexer.server.auth.user_manager import User, UserRole

    client_id = secrets.token_hex(8)
    client_secret = secrets.token_hex(16)
    user_id = secrets.token_hex(4)
    user = User(
        username="story1491",
        # Never read by this path; generated so no fixed value is committed.
        password_hash=secrets.token_hex(8),
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    manager = _SingleUserManager(user_id, user)

    monkeypatch.setattr(
        deps_module,
        "mcp_credential_manager",
        _RealBcryptCredentialStore(client_id, client_secret, user_id),
    )
    monkeypatch.setattr(deps_module, "user_manager", manager)
    monkeypatch.setattr(deps_module, "elevated_session_manager", None)
    return httpx.BasicAuth(client_id, client_secret), manager


def _app_recording_loop_thread(loop_thread_idents: List[int]) -> FastAPI:
    """App whose /whoami depends on the REAL auth dependency.

    The route records the thread it runs on -- definitively the event-loop
    thread, since an async route body always executes there.
    """
    from fastapi import Depends

    import code_indexer.server.auth.dependencies as deps_module
    from code_indexer.server.auth.user_manager import User

    app = _app_with_probe()

    @app.get("/whoami")
    async def _whoami(
        current_user: User = Depends(deps_module.get_current_user_for_mcp),
    ) -> JSONResponse:
        loop_thread_idents.append(threading.get_ident())
        return JSONResponse(content={"username": current_user.username})

    return app


@pytest.mark.asyncio
async def test_ac1_user_lookup_runs_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: the synchronous user-DB read must leave the event loop too.

    AC1's technical requirements list THREE blocking calls, not one: bcrypt
    ``verify_credential``, the ``elevated_session_manager.create`` DB write, and
    the synchronous user read. Offloading only bcrypt leaves
    ``user_manager.get_user(user_id)`` running on the loop thread immediately
    afterwards.

    Proven by thread identity, not timing.
    """
    auth, manager = _wire_real_bcrypt_auth(monkeypatch)
    loop_thread_idents: List[int] = []
    app = _app_recording_loop_thread(loop_thread_idents)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_TEST_BASE_URL
    ) as client:
        resp = await client.get("/whoami", auth=auth)

    assert resp.status_code == 200
    assert resp.json() == {"username": "story1491"}
    recorded = manager.recorded_thread_idents()
    assert recorded, "user_manager.get_user was never called"
    assert loop_thread_idents, "the async route never ran"

    loop_thread = loop_thread_idents[0]
    assert all(ident != loop_thread for ident in recorded), (
        f"user_manager.get_user ran on the event-loop thread ({recorded} vs "
        f"loop {loop_thread}) -- AC1 requires the synchronous user DB read to "
        "be offloaded alongside bcrypt"
    )


def _mcp_auth_app(
    monkeypatch: pytest.MonkeyPatch,
) -> Tuple[FastAPI, httpx.BasicAuth, _RealBcryptCredentialStore]:
    """Real app whose /whoami route depends on the REAL auth dependency.

    Returns the app, an ``httpx.BasicAuth`` for the request -- httpx's own
    supported credential mechanism, so no authorization header is hand-built --
    and the credential store, whose ``barrier`` the caller sets per measurement.
    The identifier and secret are generated per run and exist only in memory.
    """
    from fastapi import Depends

    import code_indexer.server.auth.dependencies as deps_module
    from code_indexer.server.auth.user_manager import User, UserRole

    client_id = f"mcp_{secrets.token_hex(8)}"
    client_secret = secrets.token_hex(16)
    user_id = f"user_{secrets.token_hex(4)}"
    user = User(
        username="story1491",
        password_hash=_PLACEHOLDER_PASSWORD_HASH,
        role=UserRole.ADMIN,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    store = _RealBcryptCredentialStore(client_id, client_secret, user_id)
    monkeypatch.setattr(deps_module, "mcp_credential_manager", store)
    monkeypatch.setattr(deps_module, "user_manager", _SingleUserManager(user_id, user))
    # Elevation-window creation is a separate DB boundary with its own AC1
    # offload; disabling it here keeps this measurement focused on bcrypt.
    monkeypatch.setattr(deps_module, "elevated_session_manager", None)

    app = _app_with_probe()

    @app.get("/whoami")
    async def _whoami(
        current_user: User = Depends(deps_module.get_current_user_for_mcp),
    ) -> JSONResponse:
        return JSONResponse(content={"username": current_user.username})

    @app.get("/whoami-on-loop")
    async def _whoami_on_loop() -> JSONResponse:
        # The pre-change shape: the SAME real bcrypt verification the
        # dependency performs, executed directly on the event loop.
        verified_user_id = store.verify_credential(client_id, client_secret)
        return JSONResponse(
            content={"username": user.username if verified_user_id else None}
        )

    return app, httpx.BasicAuth(client_id, client_secret), store


@pytest.mark.asyncio
async def test_ac1_mcp_auth_bcrypt_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real bcrypt in the REAL auth dependency must not stall other requests.

    Report Finding B1 (rank 1, "the single most impactful finding of the
    audit"): ``get_current_user_for_mcp`` ran bcrypt credential verification --
    100-300 ms of pure, GIL-held CPU -- directly on the event loop for EVERY
    OAuth/Basic MCP request.  The fix wraps it in
    ``anyio.to_thread.run_sync``.

    This drives the genuine production dependency over a real Basic-auth
    request with REAL bcrypt (never a mock, never a sleep), and compares the
    concurrent-probe latency against the identical real verification performed
    on the loop.  Each measurement gets its own barrier, entered by the store at
    the instant real bcrypt work begins, so both are anchored identically.
    """
    app, auth, store = _mcp_auth_app(monkeypatch)

    async def _on_loop(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/whoami-on-loop")

    async def _via_dependency(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/whoami", auth=auth)

    before_barrier = _BlockingBarrier()
    store.barrier = before_barrier
    before = await _measure_probe_latency_during(app, _on_loop, barrier=before_barrier)

    after_barrier = _BlockingBarrier()
    store.barrier = after_barrier
    after = await _measure_probe_latency_during(
        app, _via_dependency, barrier=after_barrier
    )

    # Identical authentication outcome either way.
    assert _slow_response(before).json() == {"username": "story1491"}
    assert _slow_response(after).json() == {"username": "story1491"}

    before_probe = _max_probe_latency(before)
    after_probe = _max_probe_latency(after)
    _record(
        "ac1_mcp_auth_bcrypt",
        {
            "bcrypt_rounds": _BCRYPT_ROUNDS,
            "before_on_loop_total_wall_s": _total_wall(before),
            "before_on_loop_probe_latencies_s": _probe_latencies(before),
            "before_on_loop_max_probe_latency_s": before_probe,
            "after_real_dependency_total_wall_s": _total_wall(after),
            "after_real_dependency_probe_latencies_s": _probe_latencies(after),
            "after_real_dependency_max_probe_latency_s": after_probe,
            "probe_budget_s": _PROMPT_LATENCY_BUDGET_SECONDS,
        },
    )

    _assert_probe_was_prompt(after, "the real MCP auth dependency's bcrypt")
    assert after_probe * _MIN_PROBE_IMPROVEMENT_RATIO < before_probe, (
        "the auth dependency did not measurably free the event loop: probe "
        f"latency {after_probe:.4f}s vs on-loop {before_probe:.4f}s"
    )


def test_ac1_auth_dependency_source_offloads_each_blocking_call() -> None:
    """Each blocking call AC1 names must be offloaded, in its own function.

    Pins the specific production source regions rather than the module as a
    whole: the bcrypt verification and the elevation-window write live in
    ``get_mcp_user_from_credentials``, and the synchronous user/blacklist read
    lives in ``get_current_user_for_mcp``.
    """
    import inspect

    from code_indexer.server.auth import dependencies as deps_module

    credentials_source = inspect.getsource(deps_module.get_mcp_user_from_credentials)
    assert "verify_credential" in credentials_source
    assert credentials_source.count("anyio.to_thread.run_sync") >= 2, (
        "both the bcrypt verification and the elevated-session DB write must be offloaded (Story #1491 AC1)"
    )

    mcp_source = inspect.getsource(deps_module.get_current_user_for_mcp)
    assert "get_current_user" in mcp_source
    assert "anyio.to_thread.run_sync" in mcp_source, (
        "the synchronous get_current_user DB/blacklist read must be offloaded"
    )


# ===========================================================================
# AC5 -- research-assistant polling route (routers/research_assistant.py)
# ===========================================================================

_RA_JOB_ID = "story1491-job"
_RA_SESSION_ID = "story1491-session"
_RA_ASSISTANT_MARKDOWN = "## heading\n\nsome **bold** text\n"

# The route touches both blocking boundaries report Finding B5 names, so each
# stand-in blocks for half the total budget rather than one carrying it all.
_RA_BOUNDARY_BLOCK_SECONDS = _SLOW_SECONDS / 2


class _BlockingJobTracker:
    """JobTracker stand-in at the DB boundary: a real blocking lookup.

    Report Finding B5 names this exact query ("sync JobTracker DB query") as
    part of what poll_job ran on the event loop every 1-2 s per open page.
    Returning None makes the real route fall through to its message-store path,
    so the rest of the production logic runs unchanged.
    """

    def __init__(self, barrier: _BlockingBarrier) -> None:
        self._barrier = barrier

    def get_job(self, job_id: str) -> None:
        # First of the route's two blocking boundaries: anchor the probes here.
        self._barrier.enter()
        time.sleep(_RA_BOUNDARY_BLOCK_SECONDS)
        return None


class _BlockingMessageStore:
    """Message-store stand-in: the sync SQLite message read Finding B5 names."""

    def __init__(self, barrier: _BlockingBarrier) -> None:
        self._barrier = barrier

    def get_messages(self, session_id: str) -> List[Dict[str, str]]:
        self._barrier.enter()
        time.sleep(_RA_BOUNDARY_BLOCK_SECONDS)
        if session_id != _RA_SESSION_ID:
            return []
        return [
            {"role": "user", "content": "question?", "created_at": "2024-01-01"},
            {
                "role": "assistant",
                "content": _RA_ASSISTANT_MARKDOWN,
                "created_at": "2024-01-01",
            },
        ]


def _research_assistant_app(
    monkeypatch: pytest.MonkeyPatch, barrier: _BlockingBarrier
) -> FastAPI:
    """Real app exposing the REAL poll_job route + /ping.

    Only external boundaries are stood in for: the GitHub token source, the
    JobTracker DB, the message store, and the admin-session dependency.  The
    route body, the real ResearchAssistantService, and the real markdown
    rendering and Jinja template render all execute for real.  Both blocking
    boundaries share the caller's barrier, so whichever the route reaches first
    anchors the probes.
    """
    from code_indexer.server.routers import research_assistant as ra_module
    from code_indexer.server.web.auth import require_admin_session

    monkeypatch.setattr(ra_module, "_get_github_token", lambda: None)
    monkeypatch.setattr(
        ra_module, "_get_job_tracker", lambda: _BlockingJobTracker(barrier)
    )
    monkeypatch.setattr(
        ra_module,
        "_get_research_backend",
        lambda request: _BlockingMessageStore(barrier),
    )

    app = _app_with_probe()
    app.get("/poll/{job_id}")(ra_module.poll_job)
    app.dependency_overrides[require_admin_session] = lambda: {"username": "admin"}
    return app


@pytest.mark.asyncio
async def test_ac5_research_assistant_poll_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL poll_job route must not stall concurrent requests.

    Report Finding B5: ``poll_job`` fires every 1-2 s per open admin page and
    performed a sync JobTracker DB query, a sync SQLite message read, and
    python-markdown rendering per message.  It is a plain ``def`` route, so
    FastAPI dispatches it to its threadpool -- this asserts that actually holds
    end-to-end by blocking inside both of the real route's DB boundaries and
    measuring a concurrent request.
    """
    barrier = _BlockingBarrier()
    app = _research_assistant_app(monkeypatch, barrier)

    async def _poll(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            f"/poll/{_RA_JOB_ID}", params={"session_id": _RA_SESSION_ID}
        )

    measured = await _measure_probe_latency_during(app, _poll, barrier=barrier)
    resp = _slow_response(measured)
    assert resp.status_code == 200
    # The real markdown rendering ran on the real message content.
    assert "<strong>bold</strong>" in resp.text

    _record(
        "ac5_research_assistant_poll",
        {
            "per_boundary_block_s": _RA_BOUNDARY_BLOCK_SECONDS,
            "measured_probe_latencies_s": _probe_latencies(measured),
            "measured_max_probe_latency_s": _max_probe_latency(measured),
            "probe_budget_s": _PROMPT_LATENCY_BUDGET_SECONDS,
        },
    )
    _assert_probe_was_prompt(measured, "the real research-assistant poll route")


def test_ac5_research_assistant_routes_are_sync_dispatched() -> None:
    """poll_job and its same-pattern siblings must be plain def (AC5).

    FastAPI runs a plain ``def`` route in its threadpool, so declaring these
    routes sync IS the fix; none of them await anything essential.  If one is
    ever re-declared ``async def``, its synchronous DB/markdown work silently
    returns to the event loop.
    """
    from code_indexer.server.routers import research_assistant as ra_module

    for route_name in (
        "poll_job",
        "get_research_assistant_page",
        "load_session",
        "upload_file",
        "download_file",
    ):
        handler = getattr(ra_module, route_name)
        assert not asyncio.iscoroutinefunction(handler), (
            f"{route_name} must be a sync def route so FastAPI threadpools it "
            "(Story #1491 AC5)"
        )
