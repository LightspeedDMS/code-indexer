"""
Discriminating tests for Bug #1842 Finding 1 (CRITICAL): DependencyMapService
leaks the route-pre-claimed SharedJobSentinel on early-return paths that fire
BEFORE the sentinel object is constructed.

web/dependency_map_routes.py's trigger_dependency_map() ALWAYS pre-claims the
'analysis' sentinel synchronously (pre_claimed=True) before spawning the
worker thread, and only releases it itself if thread.start() raises -- never
when the worker's own internal early-return logic fires. Inside
run_full_analysis()/_run_delta_analysis_impl(), three branches return/raise
BEFORE the SharedJobSentinel object used for the finally-block release is
ever constructed:
  1. write-lock-skip (acquire_write_lock() returns False)
  2. lifecycle preflight exception (LifecycleBatchRunner.run() raises)
  3. self._lock busy (another same-process run already holds it)

When any of these fire with pre_claimed=True, the caller's pre-claimed
sentinel is never released -- it leaks for up to ANALYSIS_STALE_TIMEOUT_SECONDS
(4 hours), during which is_available() incorrectly reports the analysis as
still running, and cancel_running_analysis() (which only probes self._lock,
never touched on these early paths) cannot help. This is the exact,
previously-misdiagnosed root cause of test_13_depmap_coordination_1133.py's
"is_available() still False after bounded wait and cancel" failure.

Uses a REAL SharedJobSentinel (Messi Rule #1 anti-mock), pre-claimed exactly
as the route layer does, for every test.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.dependency_map_service import (
    ANALYSIS_STALE_TIMEOUT_SECONDS,
    DependencyMapService,
)
from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel

_ROUTE_JOB_ID = "route-preclaimed-job"
_ROUTE_NODE_ID = "route-node"


def _make_service(
    golden_repos_dir,
    refresh_scheduler=None,
    job_tracker=None,
    lifecycle_invoker=None,
    lifecycle_debouncer=None,
):
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(golden_repos_dir)
    golden_repos_manager.list_golden_repos.return_value = [{"alias": "repo1"}]

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
        refresh_scheduler=refresh_scheduler,
        job_tracker=job_tracker,
        lifecycle_invoker=lifecycle_invoker,
        lifecycle_debouncer=lifecycle_debouncer,
    )


def _preclaim_sentinel(golden_repos_dir):
    """Pre-claim the 'analysis' sentinel exactly as the route layer does
    (real SharedJobSentinel, real O_CREAT|O_EXCL file)."""
    sentinel_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map"
    sentinel = SharedJobSentinel(sentinel_dir, ANALYSIS_STALE_TIMEOUT_SECONDS)
    claim = sentinel.try_claim("analysis", _ROUTE_JOB_ID, _ROUTE_NODE_ID)
    assert claim.success is True
    return sentinel


def _assert_sentinel_released(golden_repos_dir):
    """Fresh, independent check that the 'analysis' sentinel is no longer
    active -- used when is_available() cannot be trusted directly (e.g. the
    test deliberately holds self._lock to simulate same-process contention,
    which would make is_available() return False for an unrelated reason)."""
    sentinel_dir = Path(golden_repos_dir) / "cidx-meta" / "dependency-map"
    check_sentinel = SharedJobSentinel(sentinel_dir, ANALYSIS_STALE_TIMEOUT_SECONDS)
    assert check_sentinel.read_active("analysis") is None, (
        "pre-claimed sentinel was leaked"
    )


class TestWriteLockSkipLeaksSentinel:
    """Branch 1: acquire_write_lock() returns False."""

    def test_run_full_analysis_releases_preclaimed_sentinel_on_write_lock_skip(
        self, tmp_path
    ):
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False
        service = _make_service(tmp_path, refresh_scheduler=refresh_scheduler)
        _preclaim_sentinel(tmp_path)

        result = service.run_full_analysis(job_id=_ROUTE_JOB_ID, pre_claimed=True)

        assert result is not None and result.get("status") == "skipped"
        assert service.is_available() is True, (
            "pre-claimed sentinel was leaked -- is_available() should be "
            "True once the worker gives up without ever starting real work"
        )

    def test_run_delta_analysis_releases_preclaimed_sentinel_on_write_lock_skip(
        self, tmp_path
    ):
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False
        service = _make_service(tmp_path, refresh_scheduler=refresh_scheduler)
        _preclaim_sentinel(tmp_path)

        result = service.run_delta_analysis(job_id=_ROUTE_JOB_ID, pre_claimed=True)

        assert result is None
        assert service.is_available() is True, (
            "pre-claimed sentinel was leaked on the delta write-lock-skip path"
        )


@pytest.mark.parametrize("method_name", ["run_full_analysis", "run_delta_analysis"])
class TestLifecyclePreflightExceptionLeaksSentinel:
    """Branch 2: LifecycleBatchRunner.run() raises during the preflight."""

    def test_releases_preclaimed_sentinel_on_preflight_exception(
        self, method_name, tmp_path
    ):
        service = _make_service(
            tmp_path,
            refresh_scheduler=MagicMock(
                acquire_write_lock=MagicMock(return_value=True)
            ),
            job_tracker=MagicMock(),
            lifecycle_invoker=MagicMock(),
            lifecycle_debouncer=MagicMock(),
        )
        _preclaim_sentinel(tmp_path)

        with (
            patch(
                "code_indexer.server.services.dependency_map_service.LifecycleFleetScanner"
            ) as mock_scanner_cls,
            patch(
                "code_indexer.server.services.dependency_map_service.LifecycleBatchRunner"
            ) as mock_runner_cls,
        ):
            mock_scanner_cls.return_value.find_broken_or_missing.return_value = [
                "repo1"
            ]
            mock_runner_cls.return_value.run.side_effect = RuntimeError(
                "lifecycle repair failed"
            )

            with pytest.raises(RuntimeError, match="lifecycle repair failed"):
                getattr(service, method_name)(job_id=_ROUTE_JOB_ID, pre_claimed=True)

        assert service.is_available() is True, (
            f"pre-claimed sentinel was leaked when the lifecycle preflight "
            f"raised inside {method_name}"
        )


@pytest.mark.parametrize("method_name", ["run_full_analysis", "run_delta_analysis"])
class TestInProcessLockBusyLeaksSentinel:
    """Branch 3: self._lock is already held (same-process contention)."""

    def test_releases_preclaimed_sentinel_when_lock_busy(self, method_name, tmp_path):
        service = _make_service(tmp_path, refresh_scheduler=None)
        _preclaim_sentinel(tmp_path)
        acquired = service._lock.acquire(blocking=False)
        assert acquired is True
        try:
            if method_name == "run_full_analysis":
                with pytest.raises(RuntimeError):
                    service.run_full_analysis(job_id=_ROUTE_JOB_ID, pre_claimed=True)
            else:
                result = service.run_delta_analysis(
                    job_id=_ROUTE_JOB_ID, pre_claimed=True
                )
                assert result is None

            _assert_sentinel_released(tmp_path)
        finally:
            service._lock.release()
