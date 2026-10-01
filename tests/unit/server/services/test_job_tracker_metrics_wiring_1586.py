"""Story #1586 AC3: cidx.jobs.* OTEL metrics wired into JobTracker lifecycle.

Proves the WIRING -- a real call into JobTracker.complete_job/fail_job emits
real cidx.jobs.completed/cidx.jobs.failed/cidx.jobs.duration OTEL metrics via
JobMetrics -- not just that JobMetrics.record_job_completed/record_job_failed
work standalone (already covered in tests/unit/server/telemetry/test_job_metrics.py).

Uses a REAL JobTracker backed by a real SQLite database (the `tracker`/
`db_path` fixtures from the local conftest.py) and a real JobMetrics instance
installed as the process-wide singleton via active_job_metrics_singleton()
(real OTEL SDK MeterProvider + InMemoryMetricReader) -- MESSI Rule #1: no
mocks of the code under test.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.job_tracker import TrackedOperation

from tests.unit.server.telemetry.otel_test_support import (
    active_job_metrics_singleton,
    find_metric,
)


def _register_and_run(tracker, job_id: str, operation_type: str) -> None:
    """Register a job and transition it to 'running' -- shared setup for
    every complete_job/fail_job wiring test below."""
    tracker.register_job(job_id, operation_type, "admin")
    tracker.update_status(job_id, status="running")


def _first_data_point(reader, metric_name: str):
    """Return the single OTEL data point for metric_name, asserting it was
    emitted at all (fails loudly with the metric name on a miss)."""
    metric = find_metric(reader, metric_name)
    assert metric is not None, f"{metric_name} not emitted"
    return list(metric.data.data_points)[0]


class TestJobTrackerCompleteJobMetricsWiring:
    def test_complete_job_records_completed_counter_and_duration(self, tracker):
        with active_job_metrics_singleton() as (_metrics, reader):
            _register_and_run(tracker, "job-metrics-comp-1", "dep_map_analysis")
            tracker.complete_job("job-metrics-comp-1")

            dp = _first_data_point(reader, "cidx.jobs.completed")
            assert dp.value == 1
            assert dp.attributes["job_type"] == "dep_map_analysis"
            assert dp.attributes["status"] == "completed"

            duration_dp = _first_data_point(reader, "cidx.jobs.duration")
            assert duration_dp.attributes["job_type"] == "dep_map_analysis"
            assert duration_dp.attributes["status"] == "completed"
            assert duration_dp.sum >= 0.0

    def test_complete_job_does_not_emit_failed_counter(self, tracker):
        with active_job_metrics_singleton() as (_metrics, reader):
            _register_and_run(tracker, "job-metrics-comp-2", "golden_repo_refresh")
            tracker.complete_job("job-metrics-comp-2")

            assert find_metric(reader, "cidx.jobs.failed") is None


class TestJobTrackerFailJobMetricsWiring:
    def test_fail_job_records_failed_counter_with_explicit_error_type(self, tracker):
        with active_job_metrics_singleton() as (_metrics, reader):
            _register_and_run(tracker, "job-metrics-fail-1", "dep_map_analysis")
            tracker.fail_job(
                "job-metrics-fail-1", error="boom", error_type="RuntimeError"
            )

            dp = _first_data_point(reader, "cidx.jobs.failed")
            assert dp.value == 1
            assert dp.attributes["job_type"] == "dep_map_analysis"
            assert dp.attributes["error_type"] == "RuntimeError"
            assert dp.attributes["status"] == "failed"

            duration_dp = _first_data_point(reader, "cidx.jobs.duration")
            assert duration_dp.attributes["job_type"] == "dep_map_analysis"
            assert duration_dp.attributes["error_type"] == "RuntimeError"
            assert duration_dp.attributes["status"] == "failed"

    def test_fail_job_defaults_error_type_to_unknown_when_omitted(self, tracker):
        """Backward compatibility: existing fail_job(job_id, error) callers
        (no error_type kwarg) must keep working and bucket under 'unknown'
        rather than raising or requiring a new required field."""
        with active_job_metrics_singleton() as (_metrics, reader):
            _register_and_run(tracker, "job-metrics-fail-2", "golden_repo_refresh")
            tracker.fail_job("job-metrics-fail-2", error="disk full")

            dp = _first_data_point(reader, "cidx.jobs.failed")
            assert dp.attributes["error_type"] == "unknown"

    def test_fail_job_does_not_emit_completed_counter(self, tracker):
        with active_job_metrics_singleton() as (_metrics, reader):
            _register_and_run(tracker, "job-metrics-fail-3", "dep_map_analysis")
            tracker.fail_job("job-metrics-fail-3", error="boom")

            assert find_metric(reader, "cidx.jobs.completed") is None


class TestNoPrematureTelemetrySingletonCreation:
    """Regression test: a background thread (e.g. golden-repos startup
    reconciliation) can call complete_job/fail_job BEFORE the main lifespan
    coroutine reaches its dedicated telemetry-init block. get_telemetry_manager()
    is documented as "first call wins, creates a disabled fallback config if
    None" -- so an early bare get_telemetry_manager() call from
    _record_job_metric would win that race and permanently poison telemetry
    to disabled for the whole server process (confirmed via a live-stack
    trace during Phase 2 integration testing: global_repos_lifecycle.py's
    _run_reconcile background thread calling complete_job triggered exactly
    this). complete_job/fail_job must therefore only PEEK at an
    already-initialized TelemetryManager, never create one.
    """

    @staticmethod
    def _run_terminal_transition(tracker, job_id: str, *, should_fail: bool) -> None:
        """Shared regression-test body: register+run a job, then drive it to
        the given terminal transition (complete or fail)."""
        _register_and_run(tracker, job_id, "dep_map_analysis")
        if should_fail:
            tracker.fail_job(job_id, error="boom")
        else:
            tracker.complete_job(job_id)

    def test_terminal_transitions_do_not_create_telemetry_singleton_when_absent(
        self, tracker
    ):
        from code_indexer.server.telemetry.manager import (
            peek_telemetry_manager,
            reset_telemetry_manager,
        )

        reset_telemetry_manager()
        try:
            assert peek_telemetry_manager() is None

            self._run_terminal_transition(
                tracker, "job-no-premature-init-complete", should_fail=False
            )
            assert peek_telemetry_manager() is None, (
                "complete_job must not eagerly create the TelemetryManager "
                "singleton before the real startup config is loaded"
            )

            self._run_terminal_transition(
                tracker, "job-no-premature-init-fail", should_fail=True
            )
            assert peek_telemetry_manager() is None, (
                "fail_job must not eagerly create the TelemetryManager "
                "singleton before the real startup config is loaded"
            )
        finally:
            reset_telemetry_manager()


class TestTrackedOperationErrorTypeWiring:
    """Story #1586 code-review round 2 gap: AC3 asks for error_type to be
    'derived from the exception class name at the call site' when a real
    exception object is available. Every fail_job() caller in src/ passed
    only error= before this fix, so the "unknown" fallback was the only
    path ever exercised in production -- the cidx.jobs.failed metric's
    error_type dimension carried zero real information.

    TrackedOperation.__exit__ is the one call site with a real exception
    object always in scope (the context manager protocol hands it
    exc_val directly), making it the natural, minimal-risk site to wire
    up first.
    """

    def test_tracked_operation_exit_passes_real_exception_class_name_as_error_type(
        self, tracker
    ):
        with active_job_metrics_singleton() as (_metrics, reader):
            with pytest.raises(ValueError):
                with TrackedOperation(
                    tracker,
                    "job-tracked-op-error-type",
                    "dep_map_analysis",
                    "admin",
                ):
                    raise ValueError("boom")

            dp = _first_data_point(reader, "cidx.jobs.failed")
            assert dp.attributes["error_type"] == "ValueError", (
                "TrackedOperation.__exit__ must derive error_type from the "
                "real exception's class name, not fall back to 'unknown' "
                "when a real exception is available at the call site"
            )
