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
        self, cache_backend, tmp_path: Path
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
        bgm = BackgroundJobManager(storage_path=str(tmp_path / "jobs.json"))
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

    claim_job_slot() is compare-and-swap-if-empty: calling it again with
    the real id after the slot is already occupied by the placeholder is a
    silent no-op, so the mismatch previously persisted permanently.
    """

    def test_cache_slot_holds_real_bgm_job_id_after_submission(
        self, cache_backend, tmp_path: Path
    ) -> None:
        """The cache slot's job_id must equal the job_id BGM is tracking."""
        bgm = BackgroundJobManager(storage_path=str(tmp_path / "jobs.json"))
        dashboard_service = _FakeDashboardService()

        returned_job_id = _submit_dashboard_job(
            cache_backend, bgm, dashboard_service, job_tracker=None
        )

        assert returned_job_id is not None

        cached = cache_backend.get_cached()
        assert cached is not None, "cache slot must exist after submission"
        assert cached.get("job_id") == returned_job_id, (
            f"Cache slot job_id {cached.get('job_id')!r} must match the real "
            f"BGM-tracked job_id {returned_job_id!r} -- a mismatch means the "
            f"dashboard polls a job id BGM has never heard of."
        )

        # Confirm BGM genuinely tracks the id the cache slot points to.
        tracked = bgm.get_job_status(
            cached["job_id"], username=_SYSTEM_USERNAME, is_admin=True
        )
        assert tracked is not None, (
            f"BGM has no record of cache-slot job_id {cached['job_id']!r} -- "
            "the cache slot points at a phantom job."
        )


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
        self, cache_backend, tmp_path: Path
    ) -> None:
        bgm = BackgroundJobManager(storage_path=str(tmp_path / "jobs.json"))
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
