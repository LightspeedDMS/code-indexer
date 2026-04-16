"""
Unit tests for DependencyMapDashboardJobRunner (Story #684 Phase 3).

Anti-mock methodology: uses real DependencyMapDashboardCacheBackend backed
by a real SQLite file. Only the dashboard_service and job_tracker are faked
via simple test doubles (no Mock) to keep tests deterministic and fast.
"""

import json
from typing import Any, Dict, Optional

import pytest

from code_indexer.server.storage.sqlite_backends import (
    DependencyMapDashboardCacheBackend,
)
from code_indexer.server.services.dependency_map_dashboard_job_runner import (
    DependencyMapDashboardJobRunner,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles (no Mock — real lightweight classes)
# ─────────────────────────────────────────────────────────────────────────────


class FakeDashboardService:
    """Returns a fixed result from get_job_status, optionally recording callbacks."""

    def __init__(self, result: Dict[str, Any], raise_error: Optional[Exception] = None):
        self._result = result
        self._raise_error = raise_error
        self.recorded_callbacks: list = []

    def get_job_status(self, progress_callback=None) -> Dict[str, Any]:
        if progress_callback is not None:
            self.recorded_callbacks.append(progress_callback)
            # Simulate 2 progress calls: 1/3 then 3/3
            progress_callback(1, 3)
            progress_callback(3, 3)
        if self._raise_error is not None:
            raise self._raise_error
        return self._result


class FakeTracker:
    """Records update_status calls for assertion."""

    def __init__(self):
        self.calls: list = []  # [(job_id, kwargs), ...]

    def update_status(self, job_id: str, **kwargs) -> None:
        self.calls.append((job_id, dict(kwargs)))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────


SAMPLE_RESULT: Dict[str, Any] = {
    "health": "Healthy",
    "color": "GREEN",
    "status": "completed",
}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "job_runner_test.db")


@pytest.fixture
def cache_backend(db_path):
    return DependencyMapDashboardCacheBackend(db_path)


def make_runner(
    cache_backend: DependencyMapDashboardCacheBackend,
    result: Optional[Dict[str, Any]] = None,
    raise_error: Optional[Exception] = None,
) -> tuple:
    """Return (runner, tracker, service) pre-wired for a single test."""
    effective_result = dict(SAMPLE_RESULT) if result is None else result
    tracker = FakeTracker()
    service = FakeDashboardService(result=effective_result, raise_error=raise_error)
    runner = DependencyMapDashboardJobRunner(cache_backend, service, tracker)
    return runner, tracker, service


# ─────────────────────────────────────────────────────────────────────────────
# TestSuccessPath
# ─────────────────────────────────────────────────────────────────────────────


class TestSuccessPath:
    def test_result_is_cached_after_run(self, cache_backend):
        runner, _, _ = make_runner(cache_backend)
        runner.run("job-1")
        cached = cache_backend.get_cached()
        assert cached is not None
        assert json.loads(cached["result_json"]) == SAMPLE_RESULT

    def test_tracker_receives_running_then_completed(self, cache_backend):
        runner, tracker, _ = make_runner(cache_backend)
        runner.run("job-1")
        statuses = [call[1].get("status") for call in tracker.calls]
        assert statuses[0] == "running"
        assert statuses[-1] == "completed"

    def test_tracker_receives_100_progress_on_completion(self, cache_backend):
        runner, tracker, _ = make_runner(cache_backend)
        runner.run("job-1")
        last_call_kwargs = tracker.calls[-1][1]
        assert last_call_kwargs.get("progress") == 100

    def test_job_id_cleared_in_cache_after_success(self, cache_backend):
        cache_backend.claim_job_slot("job-1")
        runner, _, _ = make_runner(cache_backend)
        runner.run("job-1")
        cached = cache_backend.get_cached()
        assert cached is not None
        # set_cached clears job_id
        assert cached["job_id"] is None


# ─────────────────────────────────────────────────────────────────────────────
# TestFailurePath
# ─────────────────────────────────────────────────────────────────────────────


class TestFailurePath:
    def test_cache_marks_failed_on_exception(self, cache_backend):
        runner, _, _ = make_runner(
            cache_backend, raise_error=RuntimeError("analysis exploded")
        )
        with pytest.raises(RuntimeError):
            runner.run("job-2")
        cached = cache_backend.get_cached()
        assert cached is not None
        assert "analysis exploded" in cached["last_failure_message"]

    def test_tracker_receives_failed_status_on_exception(self, cache_backend):
        runner, tracker, _ = make_runner(
            cache_backend, raise_error=ValueError("bad data")
        )
        with pytest.raises(ValueError):
            runner.run("job-3")
        statuses = [call[1].get("status") for call in tracker.calls]
        assert "failed" in statuses

    def test_tracker_receives_error_message_on_exception(self, cache_backend):
        runner, tracker, _ = make_runner(
            cache_backend, raise_error=RuntimeError("network timeout")
        )
        with pytest.raises(RuntimeError):
            runner.run("job-4")
        failed_calls = [c for c in tracker.calls if c[1].get("status") == "failed"]
        assert len(failed_calls) == 1
        assert "network timeout" in failed_calls[0][1].get("error", "")

    def test_exception_is_reraised(self, cache_backend):
        runner, _, _ = make_runner(
            cache_backend, raise_error=RuntimeError("reraise me")
        )
        with pytest.raises(RuntimeError, match="reraise me"):
            runner.run("job-5")


# ─────────────────────────────────────────────────────────────────────────────
# TestProgressCallback
# ─────────────────────────────────────────────────────────────────────────────


class TestProgressCallback:
    def test_progress_callback_is_passed_to_service(self, cache_backend):
        runner, _, service = make_runner(cache_backend)
        runner.run("job-6")
        # FakeDashboardService records callbacks passed to it
        assert len(service.recorded_callbacks) == 1

    def test_progress_pct_33_reported_for_1_of_3(self, cache_backend):
        runner, tracker, _ = make_runner(cache_backend)
        runner.run("job-7")
        # callback(1, 3) -> 33%
        running_calls = [
            c
            for c in tracker.calls
            if c[1].get("status") == "running" and c[1].get("progress") is not None
        ]
        progress_values = [c[1]["progress"] for c in running_calls]
        assert 33 in progress_values

    def test_progress_info_1_of_3_reported(self, cache_backend):
        runner, tracker, _ = make_runner(cache_backend)
        runner.run("job-8")
        # FakeDashboardService fires callback(1, 3) => progress_info "1/3"
        info_calls = [c for c in tracker.calls if c[1].get("progress_info") is not None]
        assert len(info_calls) >= 1
        assert any("1/3" in c[1]["progress_info"] for c in info_calls)

    def test_zero_total_does_not_divide_by_zero(self, cache_backend):
        """Callback with total=0 must not raise ZeroDivisionError."""

        class ZeroTotalService:
            def get_job_status(self, progress_callback=None):
                if progress_callback is not None:
                    progress_callback(0, 0)  # Edge: total=0
                return dict(SAMPLE_RESULT)

        tracker = FakeTracker()
        runner = DependencyMapDashboardJobRunner(
            cache_backend, ZeroTotalService(), tracker
        )
        # Must not raise
        runner.run("job-9")
