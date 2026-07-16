"""
Unit tests for Story #1400 (async-hybrid temporal query) BGM changes:

CRITICAL 1 -- Real separate temporal job queue:
  - submit_job gains explicit named `lane` ("ordinary"|"temporal") and
    `snapshot_ctx` params -- never forwarded to the worker function.
  - A dedicated `_temporal_pending_job_queue`, served by its own fixed pool
    of `temporal_lane_concurrency` worker threads, provides REAL isolation:
    ordinary jobs never consume temporal-lane workers and vice versa.
  - get_job_queue_metrics() gains temporal_running_count/temporal_queue_depth/
    temporal_max_concurrent + ordinary_* aliases (legacy fields preserved).

CRITICAL 2 -- job_id/cancel_check injection generalized outside the
  progress_callback branch: a worker declaring job_id or cancel_check
  (with or without progress_callback) receives the real value/callable.

TDD: written BEFORE implementation.
"""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig

pytestmark = pytest.mark.slow


def _wait_for_status(manager, job_id, statuses, timeout=5.0):
    """Poll manager.jobs (or SQLite fallback) until job reaches one of
    `statuses`, or timeout. Bounded loop (Messi #14)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        d = manager.get_job_status(job_id, username="u1")
        if d is not None:
            last = d.get("status")
            if last in statuses:
                return d
        time.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not reach {statuses} within {timeout}s (last={last})"
    )


class _ManagerFixture:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"
        self.manager = None

    def teardown_method(self):
        if self.manager is not None:
            self.manager.shutdown()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_manager(self, **config_kwargs) -> BackgroundJobManager:
        config = BackgroundJobsConfig(**config_kwargs)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )
        return self.manager


class TestSubmitJobLaneAndSnapshotCtxParams(_ManagerFixture):
    def test_default_lane_is_ordinary_and_job_completes(self):
        manager = self._make_manager()

        def worker():
            return {"status": "success"}

        job_id = manager.submit_job("test_op", worker, submitter_username="u1")
        _wait_for_status(manager, job_id, {"completed"})

    def test_submit_job_accepts_lane_temporal(self):
        manager = self._make_manager()

        def worker():
            return {"status": "success"}

        job_id = manager.submit_job(
            "temporal_query", worker, submitter_username="u1", lane="temporal"
        )
        _wait_for_status(manager, job_id, {"completed"})

    def test_invalid_lane_value_raises_value_error(self):
        manager = self._make_manager()

        def worker():
            return {"status": "success"}

        with pytest.raises(ValueError):
            manager.submit_job(
                "temporal_query", worker, submitter_username="u1", lane="bogus"
            )

    def test_lane_and_snapshot_ctx_never_forwarded_to_worker_kwargs(self):
        manager = self._make_manager()
        received: dict = {}

        def worker(**kwargs):
            received.update(kwargs)
            return {"status": "success"}

        job_id = manager.submit_job(
            "temporal_query",
            worker,
            submitter_username="u1",
            lane="temporal",
            snapshot_ctx={"foo": "bar"},
        )
        _wait_for_status(manager, job_id, {"completed"})
        assert "lane" not in received
        assert "snapshot_ctx" not in received


class TestTemporalLaneQueueIsolation(_ManagerFixture):
    """CRITICAL 1: temporal and ordinary jobs read from SEPARATE queues
    served by separate worker pools -- a saturated ordinary lane must not
    block a temporal job (and vice versa)."""

    def test_temporal_job_completes_while_ordinary_lane_fully_saturated(self):
        manager = self._make_manager(
            max_concurrent_background_jobs=1, temporal_lane_concurrency=1
        )
        ordinary_started = threading.Event()
        ordinary_release = threading.Event()

        def blocking_ordinary():
            ordinary_started.set()
            ordinary_release.wait(timeout=5.0)
            return {"status": "success"}

        def fast_temporal():
            return {"status": "success"}

        # Saturate the ONE ordinary worker slot.
        manager.submit_job("ordinary_op", blocking_ordinary, submitter_username="u1")
        assert ordinary_started.wait(timeout=2.0)

        # A second ordinary job now stays PENDING (proves the ordinary pool
        # really is saturated -- the isolation claim below is meaningful).
        pending_ordinary_id = manager.submit_job(
            "ordinary_op", fast_temporal, submitter_username="u1"
        )

        # The temporal job must complete via its OWN dedicated worker,
        # entirely independent of the saturated ordinary pool.
        temporal_id = manager.submit_job(
            "temporal_query", fast_temporal, submitter_username="u1", lane="temporal"
        )
        _wait_for_status(manager, temporal_id, {"completed"}, timeout=3.0)

        # The blocked ordinary job is still pending -- proves it was never
        # touched by the temporal-lane pool either.
        pending_status = manager.get_job_status(pending_ordinary_id, "u1")
        assert pending_status["status"] == "pending"

        ordinary_release.set()

    def test_temporal_lane_concurrency_bounds_temporal_jobs(self):
        manager = self._make_manager(temporal_lane_concurrency=1)
        started = threading.Event()
        release = threading.Event()

        def blocking_temporal():
            started.set()
            release.wait(timeout=5.0)
            return {"status": "success"}

        job1 = manager.submit_job(
            "temporal_query",
            blocking_temporal,
            submitter_username="u1",
            lane="temporal",
        )
        assert started.wait(timeout=2.0)

        job2 = manager.submit_job(
            "temporal_query",
            blocking_temporal,
            submitter_username="u1",
            lane="temporal",
        )
        # With temporal_lane_concurrency=1, job2 must stay pending while
        # job1 holds the only temporal worker.
        time.sleep(0.3)
        status2 = manager.get_job_status(job2, "u1")
        assert status2["status"] == "pending"

        release.set()
        _wait_for_status(manager, job1, {"completed"}, timeout=3.0)


class TestJobIdCancelCheckInjectionGeneralized(_ManagerFixture):
    """CRITICAL 2: job_id/cancel_check injection is NOT gated on the worker
    also declaring progress_callback."""

    def test_job_id_injected_with_progress_callback(self):
        manager = self._make_manager()
        received_job_id: list = [None]

        def worker(job_id, progress_callback):
            received_job_id[0] = job_id
            return {"status": "success"}

        job_id = manager.submit_job("temporal_query", worker, submitter_username="u1")
        _wait_for_status(manager, job_id, {"completed"})
        assert received_job_id[0] == job_id

    def test_job_id_injected_without_progress_callback(self):
        manager = self._make_manager()
        received_job_id: list = [None]

        def worker(job_id):
            received_job_id[0] = job_id
            return {"status": "success"}

        job_id = manager.submit_job("temporal_query", worker, submitter_username="u1")
        _wait_for_status(manager, job_id, {"completed"})
        assert received_job_id[0] == job_id

    def test_cancel_check_injected_without_progress_callback_reflects_cancellation(
        self,
    ):
        manager = self._make_manager()
        started = threading.Event()
        detected = threading.Event()

        def worker(cancel_check):
            started.set()
            for _ in range(150):  # bounded loop, ~3s max at 20ms/iter
                if cancel_check():
                    detected.set()
                    return {"status": "cancelled_detected"}
                time.sleep(0.02)
            return {"status": "timeout"}

        job_id = manager.submit_job("temporal_query", worker, submitter_username="u1")
        assert started.wait(timeout=2.0)
        manager.cancel_job(job_id, "u1")
        assert detected.wait(timeout=4.0), "cancel_check() never observed cancellation"


class TestGetJobQueueMetricsTemporalFields(_ManagerFixture):
    def test_metrics_include_temporal_and_ordinary_fields(self):
        manager = self._make_manager(temporal_lane_concurrency=7)
        metrics = manager.get_job_queue_metrics()
        for key in (
            "running_count",
            "queued_count",
            "max_concurrent",
            "ordinary_running_count",
            "ordinary_queue_depth",
            "ordinary_max_concurrent",
            "temporal_running_count",
            "temporal_queue_depth",
            "temporal_max_concurrent",
        ):
            assert key in metrics, f"missing metrics field: {key}"
        assert metrics["temporal_max_concurrent"] == 7

    def test_ordinary_aliases_equal_legacy_fields(self):
        manager = self._make_manager()
        metrics = manager.get_job_queue_metrics()
        assert metrics["ordinary_running_count"] == metrics["running_count"]
        assert metrics["ordinary_queue_depth"] == metrics["queued_count"]
        assert metrics["ordinary_max_concurrent"] == metrics["max_concurrent"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
