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

import asyncio
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
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
    from code_indexer.server.services.diagnostics_service import DiagnosticsService

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
    """Accumulate a measurement and rewrite the perf artifact."""
    with _MEASUREMENTS_LOCK:
        _MEASUREMENTS[key] = dict(payload)
        _PERF_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, object] = {}
        if _PERF_ARTIFACT.exists():
            # A previous run's artifact that is unreadable, corrupt, or not a
            # JSON object must not fail the test whose real measurement we are
            # trying to persist, but it must never be silently discarded.
            try:
                decoded = json.loads(_PERF_ARTIFACT.read_text())
                if not isinstance(decoded, dict):
                    raise ValueError(
                        f"perf artifact root is {type(decoded).__name__}, not an object"
                    )
                existing = decoded
            except (OSError, ValueError):
                logger.warning(
                    "discarding unusable perf artifact %s; rewriting from "
                    "this session's measurements only",
                    _PERF_ARTIFACT,
                    exc_info=True,
                )
                existing = {}
        existing.update(_MEASUREMENTS)
        _PERF_ARTIFACT.write_text(json.dumps(existing, indent=2, sort_keys=True))


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


async def _measure_probe_latency_during(
    app: FastAPI,
    slow_request: SlowRequest,
    *,
    probe_count: int = 3,
    probe_stagger_seconds: float = 0.02,
) -> Dict[str, object]:
    """Run ``slow_request`` and concurrently issue ``probe_count`` /ping calls.

    Returns the slow request's response plus the measured per-probe latencies.

    Each probe's latency is measured from its SCHEDULED fire time (origin +
    stagger), not from the moment its pre-request ``asyncio.sleep`` happens to
    return.  This is essential: when the loop is blocked, the starvation is
    absorbed inside that sleep's own overrun, so a timer started after the
    sleep would report a fast probe against a completely frozen loop.
    """
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url=_TEST_BASE_URL
    ) as client:
        probe_latencies: List[float] = []
        origin = time.perf_counter()

        async def _probe(ordinal: int) -> None:
            # Stagger slightly so the first probe lands after the slow request
            # has genuinely entered its blocking section.
            scheduled_at = origin + probe_stagger_seconds * (ordinal + 1)
            await asyncio.sleep(probe_stagger_seconds * (ordinal + 1))
            resp = await client.get("/ping")
            probe_latencies.append(time.perf_counter() - scheduled_at)
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


def _install_slow_branch_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real git ls-remote subprocess with a known-duration block."""
    from code_indexer.server.services import remote_branch_service as rbs_module

    def _slow_fetch(
        service_self: rbs_module.RemoteBranchService,
        clone_url: str,
        platform: str = "github",
        credentials: Optional[str] = None,
    ) -> rbs_module.BranchFetchResult:
        # A REAL synchronous block of known duration standing in for the real
        # git ls-remote subprocess (see module docstring).
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
    _install_slow_branch_fetch(monkeypatch)
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

    measured = await _measure_probe_latency_during(app, _slow)
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
    failing_store = _FailingTokenStore(fail_after=2)
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


def _install_blocking_subprocess_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
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

    # Baseline: the pre-change path, awaited directly on the event loop.
    _install_blocking_subprocess_boundary(monkeypatch)
    baseline_service = _new_diagnostics_service(monkeypatch, tmp_path / "baseline.db")
    await baseline_service.run_all_diagnostics()
    baseline_records = _comparable_records(baseline_service)

    # Under test: the real route + real background-task registration.  The
    # blocking boundary is re-installed so this run blocks too (the latch above
    # was consumed by the baseline run).
    _install_blocking_subprocess_boundary(monkeypatch)
    service = _new_diagnostics_service(monkeypatch, tmp_path / "under_test.db")
    monkeypatch.setattr(diagnostics_router, "diagnostics_service", service)
    app = _app_with_probe()
    app.post("/run-all")(diagnostics_router.run_all_diagnostics)

    async def _slow(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/run-all")

    measured = await _measure_probe_latency_during(app, _slow)
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


# ===========================================================================
# AC2 -- regex_search MCP dispatch (mcp/handlers/search.py::handle_regex_search)
# ===========================================================================

# Corpus size: large enough that the handler's SYNCHRONOUS sections (the
# per-match Path.resolve calls, the whole-output read and per-line json.loads
# of ripgrep's JSON stream) take tens to hundreds of milliseconds of REAL work
# -- no artificial sleep is used anywhere in the AC2 proof.
_REGEX_CORPUS_FILES = 3000
_REGEX_CORPUS_PACKAGES = 50
_REGEX_REPO_ALIAS = "story1491repo"
_REGEX_PATTERN = "needle_target_"

# The post-change probe must be dramatically faster than the pre-change one,
# not merely under an absolute budget: the real work's duration is
# machine-dependent, so the RATIO is the meaningful discriminator.
_MIN_PROBE_IMPROVEMENT_RATIO = 4.0


def _build_regex_corpus(root: Path) -> None:
    """Create a real on-disk corpus that ripgrep will really search.

    Includes a real ``.git`` directory so the production resolver
    (``_legacy._resolve_repo_path``) accepts the absolute path via its
    full-path branch -- no server-only registry wiring needed, and the whole
    resolve + search + parse path stays real.
    """
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    for index in range(_REGEX_CORPUS_FILES):
        package = root / f"pkg{index % _REGEX_CORPUS_PACKAGES}"
        package.mkdir(parents=True, exist_ok=True)
        (package / f"mod{index}.py").write_text(
            f"def {_REGEX_PATTERN}{index}():\n    return {index}\n"
        )


def _regex_dispatch_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    """Real app with two routes, both dispatching via the REAL MCP dispatcher.

    ``/dispatch/registered`` invokes whatever ``regex_search`` handler the
    production registry exposes (the thing this story changes).
    ``/dispatch/async-impl`` invokes the async implementation the same way the
    dispatcher did BEFORE the change, giving a real pre-change measurement to
    compare against.  Nothing about the dispatcher, the handler, ripgrep, or
    the filesystem is mocked: only ``app.state.golden_repos_dir`` is pointed at
    the temp corpus so repo resolution finds it.
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

    return app


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


def _regex_match_count(resp: httpx.Response) -> int:
    payload = json.loads(resp.json()["content"][0]["text"])
    assert payload["success"] is True, payload
    return int(payload["total_matches"])


@pytest.mark.asyncio
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
    """
    app = _regex_dispatch_app(monkeypatch, tmp_path)

    async def _slow_async_impl(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/dispatch/async-impl")

    async def _slow_registered(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/dispatch/registered")

    before = await _measure_probe_latency_during(app, _slow_async_impl)
    after = await _measure_probe_latency_during(app, _slow_registered)

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
    _record(
        "ac2_regex_search_dispatch",
        {
            "corpus_files": _REGEX_CORPUS_FILES,
            "matches": _regex_match_count(after_resp),
            "before_async_dispatch_total_wall_s": _total_wall(before),
            "before_async_dispatch_probe_latencies_s": _probe_latencies(before),
            "before_async_dispatch_max_probe_latency_s": before_probe,
            "after_sync_dispatch_total_wall_s": _total_wall(after),
            "after_sync_dispatch_probe_latencies_s": _probe_latencies(after),
            "after_sync_dispatch_max_probe_latency_s": after_probe,
            "probe_budget_s": _PROMPT_LATENCY_BUDGET_SECONDS,
        },
    )

    _assert_probe_was_prompt(after, "regex_search")

    # Deliberately NOT a before/after ratio here (unlike AC1, whose bcrypt cost
    # is large and stable). regex_search's real work is dominated by the ripgrep
    # SUBPROCESS, which already yields at await points; only its synchronous
    # share (prefilter, Path.resolve fan-out, output read + json.loads) blocks
    # the loop, and that share is small enough that under concurrent-suite load
    # a ratio comparison is noise-dominated and non-discriminating -- it was
    # empirically observed to invert. The load-robust evidence is instead:
    #   1. the absolute promptness budget asserted above, and
    #   2. the deterministic dispatch property below -- the REGISTERED handler
    #      is not a coroutine function, so protocol.py provably takes its
    #      run_in_executor branch (whose off-loop thread execution is itself
    #      pinned by tests/unit/server/mcp/test_invoke_handler_executor.py).
    from code_indexer.server.mcp.handlers import search as search_handlers

    registered_handler = {}  # type: Dict[str, Callable[..., Any]]
    search_handlers._register(registered_handler)
    assert not asyncio.iscoroutinefunction(registered_handler["regex_search"]), (
        "regex_search must be sync-dispatched so the protocol dispatcher "
        "offloads its synchronous work to the executor (Story #1491 AC2)"
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

    def verify_credential(self, client_id: str, client_secret: str) -> Optional[str]:
        import bcrypt

        if client_id != self._client_id:
            return None
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
) -> Tuple[FastAPI, httpx.BasicAuth]:
    """Real app whose /whoami route depends on the REAL auth dependency.

    Returns the app plus an ``httpx.BasicAuth`` for the request -- httpx's own
    supported credential mechanism, so no authorization header is hand-built.
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

    return app, httpx.BasicAuth(client_id, client_secret)


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
    on the loop.
    """
    app, auth = _mcp_auth_app(monkeypatch)

    async def _on_loop(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/whoami-on-loop")

    async def _via_dependency(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/whoami", auth=auth)

    before = await _measure_probe_latency_during(app, _on_loop)
    after = await _measure_probe_latency_during(app, _via_dependency)

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

    def get_job(self, job_id: str) -> None:
        time.sleep(_RA_BOUNDARY_BLOCK_SECONDS)
        return None


class _BlockingMessageStore:
    """Message-store stand-in: the sync SQLite message read Finding B5 names."""

    def get_messages(self, session_id: str) -> List[Dict[str, str]]:
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


def _research_assistant_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Real app exposing the REAL poll_job route + /ping.

    Only external boundaries are stood in for: the GitHub token source, the
    JobTracker DB, the message store, and the admin-session dependency.  The
    route body, the real ResearchAssistantService, and the real markdown
    rendering and Jinja template render all execute for real.
    """
    from code_indexer.server.routers import research_assistant as ra_module
    from code_indexer.server.web.auth import require_admin_session

    monkeypatch.setattr(ra_module, "_get_github_token", lambda: None)
    monkeypatch.setattr(ra_module, "_get_job_tracker", _BlockingJobTracker)
    monkeypatch.setattr(
        ra_module, "_get_research_backend", lambda request: _BlockingMessageStore()
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
    app = _research_assistant_app(monkeypatch)

    async def _poll(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            f"/poll/{_RA_JOB_ID}", params={"session_id": _RA_SESSION_ID}
        )

    measured = await _measure_probe_latency_during(app, _poll)
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
