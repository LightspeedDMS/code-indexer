"""
Regression test for Bug #1620: job_id parameter collision at the
_submit_dashboard_job -> BackgroundJobManager.submit_job seam.

Anti-mock methodology: drives the REAL BackgroundJobManager (real thread
pool dispatch, real SQLite-backed job persistence) and the REAL
DependencyMapDashboardJobRunner via the REAL _submit_dashboard_job call
site. Only dashboard_service is a lightweight fake for deterministic
results -- everything that crosses the submit_job seam is real.

This is the exact seam that was missing coverage per Bug #1620's root
cause analysis:
  - test_depmap_dashboard_job_runner.py calls runner.run() directly,
    bypassing BackgroundJobManager entirely.
  - test_dependency_map_routes_sentinel.py uses a FakeBgJobManager that
    only records submit_job() args and never actually executes func.

Neither existing test crosses the seam where BGM's signature-introspection
job_id injection collides with a positionally-passed job_id argument.
This test does, and it must fail with the real TypeError on the
pre-fix call site (verified in code review before the fix landed).

Code-review follow-up (both closed by this file):
  - BLOCKING: _get_dashboard_cache_backend() constructs
    FilesystemDashboardCacheBackend in production -- the SQLite
    DependencyMapDashboardCacheBackend has zero production instantiations.
    The seam tests are parametrized over BOTH backends via the
    `cache_backend` fixture so the backend that actually ships is exercised
    through the real seam, not just the one that happens to be convenient
    to construct in a test.
  - Non-blocking: DependencyMapDashboardJobRunner.run's except block calls
    self._tracker.fail_job(...). Pre-fix this bug never reached the worker
    body at all, so a missing _NullJobTracker.fail_job was never exercised.
    TestDashboardJobFailurePathWithNullTracker below closes that gap.
"""

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.storage.sqlite_backends import (
    DependencyMapDashboardCacheBackend,
)
from code_indexer.server.storage.filesystem_backends import (
    FilesystemDashboardCacheBackend,
)
from code_indexer.server.web.dependency_map_routes import _submit_dashboard_job

_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
_POLL_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.05
_SYSTEM_USERNAME = "system"
_FAILURE_MESSAGE = "boom-1620-failure-path"


class _FakeDashboardService:
    """Deterministic dashboard_service double -- no Mock, real callable shape."""

    def get_job_status(self, progress_callback=None) -> Dict[str, Any]:
        if progress_callback is not None:
            progress_callback(1, 1)
        return {
            "health": "Healthy",
            "color": "GREEN",
            "status": "idle",
            "last_run": None,
            "next_run": None,
            "error_message": None,
            "run_history": [],
        }


class _FailingDashboardService:
    """Deterministic dashboard_service double that always raises."""

    def get_job_status(self, progress_callback=None) -> Dict[str, Any]:
        raise ValueError(_FAILURE_MESSAGE)


class _BlockingDashboardService:
    """
    Deterministic dashboard_service double that blocks until released.

    Bug #1866: used to force a specific thread interleaving between the
    submitting thread (running _submit_dashboard_job) and the BGM worker
    thread executing DependencyMapDashboardJobRunner.run, instead of
    relying on wall-clock timing/scheduler luck to produce it.
    """

    def __init__(self, release_event: threading.Event) -> None:
        self._release_event = release_event

    def get_job_status(self, progress_callback=None) -> Dict[str, Any]:
        released = self._release_event.wait(timeout=_POLL_TIMEOUT_SECONDS)
        assert released, "test bug: release_event was never set"
        if progress_callback is not None:
            progress_callback(1, 1)
        return {
            "health": "Healthy",
            "color": "GREEN",
            "status": "idle",
            "last_run": None,
            "next_run": None,
            "error_message": None,
            "run_history": [],
        }


def _poll_until_terminal(
    bgm: BackgroundJobManager, job_id: str, timeout: float = _POLL_TIMEOUT_SECONDS
) -> Optional[Dict[str, Any]]:
    """Poll BGM's real get_job_status until the job reaches a terminal state."""
    deadline = time.time() + timeout
    job: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        job = bgm.get_job_status(job_id, username=_SYSTEM_USERNAME, is_admin=True)
        if job is not None and job.get("status") in _TERMINAL_STATUSES:
            return job
        time.sleep(_POLL_INTERVAL_SECONDS)
    return job


def _assert_job_completed(bgm: BackgroundJobManager, job_id: str) -> Dict[str, Any]:
    """Shared poll-and-assert helper for tests that expect a clean success."""
    job = _poll_until_terminal(bgm, job_id)
    assert job is not None and job.get("status") == "completed", (
        f"Expected job to reach 'completed', got status="
        f"{job.get('status') if job else None!r}"
    )
    return job


# NOTE on `cache_backend: Any` below: callers pass either one of the two real
# production backends from the parametrized `cache_backend` fixture
# (DependencyMapDashboardCacheBackend / FilesystemDashboardCacheBackend) or
# the _CorrectionDelayingCacheBackend proxy defined below. All three are
# duck-typed against the same informal get_cached()/set_job_slot()/etc.
# surface; no shared Protocol/ABC exists anywhere in this codebase for that
# surface, so `Any` is the accurate type here rather than a narrowing lie.


def _assert_job_still_in_flight(bgm: BackgroundJobManager, job_id: str) -> None:
    """Bug #1866 helper: the job must still be non-terminal at this point."""
    tracked = bgm.get_job_status(job_id, username=_SYSTEM_USERNAME, is_admin=True)
    assert tracked is not None
    assert tracked.get("status") not in _TERMINAL_STATUSES, (
        "test setup bug: job must still be in flight (non-terminal) at "
        f"this point, got status={tracked.get('status')!r}"
    )


def _assert_cache_slot_holds(cache_backend: Any, expected_job_id: str) -> None:
    """Bug #1866 helper: the cache slot must hold the real BGM job id."""
    cached = cache_backend.get_cached()
    assert cached is not None, "cache slot must exist after submission"
    assert cached.get("job_id") == expected_job_id, (
        f"Cache slot job_id {cached.get('job_id')!r} must match the real "
        f"BGM-tracked job_id {expected_job_id!r} while the job is still "
        "in flight -- a mismatch here means the dashboard would poll a "
        "job id BGM has never heard of (Bug #1620's original defect)."
    )


def _assert_no_phantom_cache_pointer(
    cache_backend: Any, bgm: BackgroundJobManager
) -> None:
    """Bug #1866 helper: the cache slot must never point at an unknown job."""
    cached = cache_backend.get_cached()
    assert cached is not None
    cache_job_id = cached.get("job_id")
    if cache_job_id is not None:
        # If the slot holds a job_id at all, it MUST be one BGM genuinely
        # tracks -- this is the actual #1620 invariant.
        tracked = bgm.get_job_status(
            cache_job_id, username=_SYSTEM_USERNAME, is_admin=True
        )
        assert tracked is not None, (
            f"cache slot points at job_id {cache_job_id!r} that "
            "BackgroundJobManager has never heard of -- this is the exact "
            "#1620 phantom-pointer defect."
        )
    assert cached.get("result_json") is not None, (
        "job reached 'completed' but no result was cached"
    )


class _CorrectionDelayingCacheBackend:
    """
    Purpose-built proxy double wrapping a real cache_backend.

    Bug #1866: delays set_job_slot() -- the submitter's placeholder-to-
    real-id correction -- until the worker's own terminal write
    (set_cached) has landed, deterministically forcing the worker-wins
    ordering instead of relying on scheduler luck. Every other call
    delegates straight through to the wrapped real backend, so this reads
    as a small dedicated collaborator rather than a runtime patch of
    production behaviour.
    """

    def __init__(self, real_backend: Any, worker_finished: threading.Event) -> None:
        self._real = real_backend
        self._worker_finished = worker_finished

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def set_cached(self, *args: Any, **kwargs: Any) -> None:
        self._real.set_cached(*args, **kwargs)
        self._worker_finished.set()

    def set_job_slot(self, *args: Any, **kwargs: Any) -> bool:
        finished = self._worker_finished.wait(timeout=_POLL_TIMEOUT_SECONDS)
        assert finished, "test bug: worker never reached set_cached"
        return bool(self._real.set_job_slot(*args, **kwargs))


@pytest.fixture(params=["sqlite", "filesystem"])
def cache_backend(request, tmp_path: Path):
    """
    Both production cache backend implementations.

    Bug #1620 code review: _get_dashboard_cache_backend() constructs
    FilesystemDashboardCacheBackend exclusively in production -- the SQLite
    DependencyMapDashboardCacheBackend has zero production instantiations
    anywhere in src/. Parametrizing every seam test over both backends
    ensures the one that actually ships is exercised, not just the one
    convenient to construct directly.
    """
    if request.param == "sqlite":
        db_path = tmp_path / "dashboard_cache.db"
        return DependencyMapDashboardCacheBackend(db_path=str(db_path))
    cache_dir = tmp_path / "dep-map-cache"
    cache_dir.mkdir()
    return FilesystemDashboardCacheBackend(cache_dir=cache_dir)


class TestDashboardJobRealBgmSeam:
    """Bug #1620: real DependencyMapDashboardJobRunner.run through real BGM."""

    def test_submit_dashboard_job_completes_through_real_bgm(
        self, cache_backend, tmp_path: Path, background_job_manager_factory
    ) -> None:
        """
        _submit_dashboard_job must produce a job that reaches status
        'completed' when driven through a REAL BackgroundJobManager.

        Pre-fix, this fails because submit_job() injects job_id as a
        keyword (since DependencyMapDashboardJobRunner.run declares a
        job_id parameter) while _submit_dashboard_job also passes its
        own job_id positionally -- both bind to the same parameter,
        raising TypeError: run() got multiple values for argument
        'job_id', and the job is marked FAILED with that TypeError
        as its error before the worker body ever executes.
        """
        bgm = background_job_manager_factory(storage_path=str(tmp_path / "jobs.json"))
        dashboard_service = _FakeDashboardService()

        job_id = _submit_dashboard_job(
            cache_backend, bgm, dashboard_service, job_tracker=None
        )

        assert job_id is not None, "_submit_dashboard_job must return a job_id"

        job = _poll_until_terminal(bgm, job_id)

        assert job is not None, f"job {job_id} was never observed by BGM"
        assert job.get("status") == "completed", (
            f"Expected job to reach 'completed', got status={job.get('status')!r} "
            f"error={job.get('error')!r}"
        )


class TestDashboardJobCacheSlotMatchesRealJobId:
    """
    Bug #1620 secondary defect: submit_job mints its OWN job_id and ignores
    the caller's positional value entirely, so the cache slot -- populated
    via cache_backend.claim_job_slot(new_job_id) BEFORE submission -- can
    end up holding a job id that BackgroundJobManager has never heard of.

    claim_job_slot() is compare-and-swap-if-empty, and the correction
    (cache_backend.set_job_slot(actual_id, expected_current=new_job_id) in
    _submit_dashboard_job) is ALSO a CAS, keyed on the placeholder still
    being present. That CAS races the BGM worker thread, which
    independently and *unconditionally* clears job_id back to NULL the
    moment the job reaches a terminal state
    (DependencyMapDashboardJobRunner.run -> cache_backend.set_cached /
    .mark_job_failed both unconditionally write job_id=NULL).

    Bug #1866: the original version of this test asserted the correction's
    result immediately after _submit_dashboard_job() returns, with no
    synchronization. That silently assumed the submitting thread always
    wins the race against the worker thread -- true on an idle box, false
    under load, where the worker can be scheduled first and reach its
    terminal write before the submitter's own very next line runs.
    Observed failures under load: cached.get("job_id") is None while
    returned_job_id is a live, running BGM job.

    Investigation established this is NOT a production defect (see
    Bug #1866 turn-8 handoff for the full trace): the front door
    (dependency_map_routes.depmap_job_status_partial, lines ~920-935)
    polls job status via tracker.get_job(job_id) using the real
    BGM-returned id directly -- never via cache_backend's job_id field.
    cache_backend.get_running_job_id() (STATE 3) reads the cache's job_id
    only as a request-coalescing optimization to avoid re-submitting a
    duplicate job while one is still in flight -- not a correctness
    dependency. And the worker's unconditional terminal write always
    represents a state that is equal-or-newer than the submitter's
    in-flight correction would have produced: set_job_slot's CAS semantics
    exist precisely so that a legitimate newer transition (job finished)
    is never clobbered by a stale in-flight correction. Losing the race is
    therefore a legitimate, harmless outcome, not corruption.

    The two tests below assert the actual invariant deterministically, for
    BOTH legitimate orderings, using a blocking dashboard-service double /
    an ordering-delaying cache-backend proxy to force each interleaving
    instead of racing the scheduler:

      1. While the job is still verifiably in flight, the cache slot must
         already show the real, corrected job_id -- the original #1620
         defect (a permanently-wrong placeholder) would fail this.
      2. If the worker's terminal write happens to land first, the cache
         slot must never be left pointing at a job_id BGM does not track
         -- the actual "phantom pointer" invariant #1620 exists to guard.
    """

    def test_cache_slot_holds_corrected_job_id_while_job_still_in_flight(
        self, cache_backend, tmp_path: Path, background_job_manager_factory
    ) -> None:
        """
        Forces the submitter-wins ordering: the worker is blocked inside
        dashboard_service.get_job_status() until after this test has
        already inspected the cache slot, so the CAS correction is
        deterministically observed rather than caught by luck.
        """
        bgm = background_job_manager_factory(storage_path=str(tmp_path / "jobs.json"))
        release_event = threading.Event()
        dashboard_service = _BlockingDashboardService(release_event)

        returned_job_id = _submit_dashboard_job(
            cache_backend, bgm, dashboard_service, job_tracker=None
        )
        assert returned_job_id is not None

        # The worker thread is still blocked inside get_job_status() and
        # therefore cannot have reached set_cached() yet -- the job is
        # provably still in flight at this exact point, not a hopeful
        # assumption about scheduling speed.
        _assert_job_still_in_flight(bgm, returned_job_id)
        _assert_cache_slot_holds(cache_backend, returned_job_id)

        release_event.set()
        _assert_job_completed(bgm, returned_job_id)

    def test_cache_slot_never_left_phantom_when_worker_completes_before_correction(
        self, cache_backend, tmp_path: Path, background_job_manager_factory
    ) -> None:
        """
        Forces the worker-wins ordering: the submitter's placeholder-to-
        real-id correction is delayed via _CorrectionDelayingCacheBackend
        until the worker's own terminal write (set_cached) has already
        landed, deterministically reproducing the interleaving that
        previously only showed up under load.
        """
        bgm = background_job_manager_factory(storage_path=str(tmp_path / "jobs.json"))
        dashboard_service = _FakeDashboardService()
        worker_finished = threading.Event()
        ordering_backend = _CorrectionDelayingCacheBackend(
            cache_backend, worker_finished
        )

        returned_job_id = _submit_dashboard_job(
            ordering_backend, bgm, dashboard_service, job_tracker=None
        )
        assert returned_job_id is not None

        _assert_job_completed(bgm, returned_job_id)
        _assert_no_phantom_cache_pointer(cache_backend, bgm)


class TestDashboardJobFailurePathWithNullTracker:
    """
    Bug #1620 code review follow-up: DependencyMapDashboardJobRunner.run's
    except block calls self._tracker.fail_job(job_id, error=...). Pre-fix,
    this bug's TypeError meant the worker body -- and therefore this except
    block -- never executed at all, so a missing _NullJobTracker.fail_job
    method was never reachable. Now that the worker body genuinely runs, a
    real dashboard_service failure with no real JobTracker available
    (job_tracker=None -> _NullJobTracker fallback) must report the REAL
    error, not an AttributeError from the missing method masking it.
    """

    def test_failure_with_null_tracker_reports_real_error_not_attributeerror(
        self, cache_backend, tmp_path: Path, background_job_manager_factory
    ) -> None:
        bgm = background_job_manager_factory(storage_path=str(tmp_path / "jobs.json"))
        dashboard_service = _FailingDashboardService()

        job_id = _submit_dashboard_job(
            cache_backend, bgm, dashboard_service, job_tracker=None
        )
        assert job_id is not None

        job = _poll_until_terminal(bgm, job_id)

        assert job is not None, f"job {job_id} was never observed by BGM"
        assert job.get("status") == "failed", (
            f"Expected 'failed' status for a raising dashboard_service, "
            f"got {job.get('status')!r}"
        )
        error_text = job.get("error") or ""
        assert _FAILURE_MESSAGE in error_text, (
            f"Expected the real dashboard_service error {_FAILURE_MESSAGE!r} "
            f"in job.error, got {error_text!r} -- an AttributeError from a "
            f"missing _NullJobTracker.fail_job would mask the real failure "
            f"reason behind '... object has no attribute ...'."
        )
