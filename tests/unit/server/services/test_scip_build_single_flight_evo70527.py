"""EVO-70527: one delegated SCIP build per repo across the whole cluster.

The in-process queue only dedupes inside one process. Two pods (or the
refresh, add-index and backfill paths) used to start the same long build
twice, and the second pod's tracker registration hit ``idx_active_job_per_repo``
and was retried on every state change. The tracker row is now an atomic
claim taken before the build is queued:

* a claim that loses to an active row means "already running elsewhere":
  nothing is queued, nothing is logged as a warning;
* a claim that wins is the one tracker row of the build;
* a registration that fails for any other reason never retries and never
  blocks the build (tracking stays best-effort).
"""

import logging
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from cidx_scip.server import lifecycle as scip_lifecycle
from code_indexer.server.services.job_tracker import DuplicateJobError
from cidx_scip.server.build_status import OPERATION, BuildTracker
from cidx_scip.server.lifecycle import (
    ScipDelegationOutcome,
    funnel_scip_generation,
)
from cidx_scip.server.resolution_queue import (
    get_scip_metrics,
    reset_scip_metrics,
)

_DELEGATED = SimpleNamespace(scip_backend="delegated-plugin")


class ClusterTracker:
    """A job tracker shared by several 'pods': one active row per (op, alias),
    enforced atomically like the ``idx_active_job_per_repo`` partial index."""

    def __init__(self) -> None:
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.calls: List[str] = []
        self._lock = threading.Lock()
        self.fail_registration_with: Optional[Exception] = None

    def register_job_if_no_conflict(
        self,
        job_id: str,
        operation_type: str,
        username: str,
        repo_alias: str,
        metadata: Optional[Dict[str, Any]] = None,
        **_: Any,
    ) -> None:
        self.calls.append("register")
        if self.fail_registration_with is not None:
            raise self.fail_registration_with
        with self._lock:
            for job in self.jobs.values():
                if (
                    job["operation_type"] == operation_type
                    and job["repo_alias"] == repo_alias
                    and job["status"] in ("pending", "running")
                ):
                    raise DuplicateJobError(operation_type, repo_alias, job["job_id"])
            self.jobs[job_id] = {
                "job_id": job_id,
                "operation_type": operation_type,
                "repo_alias": repo_alias,
                "status": "pending",
                "metadata": metadata,
            }

    def get_job(self, job_id: str) -> Optional[Any]:
        job = self.jobs.get(job_id)
        return SimpleNamespace(**job) if job else None

    def update_status(self, job_id: str, status: Optional[str] = None, **_: Any):
        self.calls.append(f"update:{status}")
        self.jobs[job_id]["status"] = status

    def complete_job(self, job_id: str, result: Any = None) -> None:
        self.jobs[job_id]["status"] = "completed"

    def fail_job(self, job_id: str, error: str = "") -> None:
        self.jobs[job_id].update(status="failed", error=error)

    def cancel_job(self, job_id: str) -> None:
        self.jobs[job_id]["status"] = "cancelled"


class CaptureQueue:
    def __init__(self, accept: bool = True) -> None:
        self.builds: List[Any] = []
        self._accept = accept

    def enqueue(self, build: Any) -> bool:
        self.builds.append(build)
        return self._accept


class RaisingQueue:
    def enqueue(self, build: Any) -> bool:
        raise RuntimeError("queue is broken")


class Generator:
    def generate(self, progress_callback=None):
        return SimpleNamespace(successful_projects=1, project_results=[])


@pytest.fixture(autouse=True)
def _metrics():
    reset_scip_metrics()
    yield
    reset_scip_metrics()


def _funnel(tmp_path, tracker, queue, monkeypatch, commit="sha1"):
    monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: commit)
    return funnel_scip_generation(
        alias="evolution",
        repo_root=tmp_path,
        config=_DELEGATED,
        golden_repo_manager=SimpleNamespace(job_tracker=tracker),
        build_queue=queue,
        generator_builder=lambda *a, **k: Generator(),
    )


class TestFunnelClaimsBeforeQueueing:
    def test_first_pod_claims_and_enqueues(self, tmp_path, monkeypatch):
        tracker, queue = ClusterTracker(), CaptureQueue()

        out = _funnel(tmp_path, tracker, queue, monkeypatch)

        assert out is ScipDelegationOutcome.ENQUEUED
        assert len(queue.builds) == 1
        [row] = tracker.jobs.values()
        assert row["operation_type"] == OPERATION
        assert row["metadata"] == {"dedupe_key": "evolution@sha1"}

    def test_second_pod_sees_a_running_build_and_queues_nothing(
        self, tmp_path, monkeypatch, caplog
    ):
        tracker = ClusterTracker()
        pod_a, pod_b = CaptureQueue(), CaptureQueue()
        _funnel(tmp_path, tracker, pod_a, monkeypatch)

        with caplog.at_level(logging.INFO):
            out = _funnel(tmp_path, tracker, pod_b, monkeypatch)

        assert out is ScipDelegationOutcome.DEDUPED
        assert pod_b.builds == []
        assert len(tracker.jobs) == 1
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert get_scip_metrics().get("delegated_deduped_cluster") == 1

    def test_a_newer_commit_is_not_built_beside_the_running_one(
        self, tmp_path, monkeypatch, caplog
    ):
        tracker = ClusterTracker()
        _funnel(tmp_path, tracker, CaptureQueue(), monkeypatch, commit="old")
        newer = CaptureQueue()

        with caplog.at_level(logging.INFO):
            out = _funnel(tmp_path, tracker, newer, monkeypatch, commit="new")

        assert out is ScipDelegationOutcome.DEDUPED
        assert newer.builds == []
        assert any(
            "evolution@old" in r.getMessage() and "evolution@new" in r.getMessage()
            for r in caplog.records
        )

    def test_a_finished_build_does_not_block_the_next_one(self, tmp_path, monkeypatch):
        tracker = ClusterTracker()
        _funnel(tmp_path, tracker, CaptureQueue(), monkeypatch)
        [job_id] = list(tracker.jobs)
        tracker.complete_job(job_id)
        queue = CaptureQueue()

        out = _funnel(tmp_path, tracker, queue, monkeypatch, commit="sha2")

        assert out is ScipDelegationOutcome.ENQUEUED
        assert len(queue.builds) == 1

    def test_a_claim_the_local_queue_refuses_is_released(self, tmp_path, monkeypatch):
        tracker = ClusterTracker()

        out = _funnel(tmp_path, tracker, CaptureQueue(accept=False), monkeypatch)

        assert out is ScipDelegationOutcome.DEDUPED
        [row] = tracker.jobs.values()
        assert row["status"] == "cancelled"

    def test_a_queue_that_raises_releases_the_claim(self, tmp_path, monkeypatch):
        """Review of !253: an exception between the claim and the queue must not
        leave a pending row that makes every later request look 'already running'."""
        tracker = ClusterTracker()

        out = _funnel(tmp_path, tracker, RaisingQueue(), monkeypatch)

        assert out is ScipDelegationOutcome.SKIPPED
        [row] = tracker.jobs.values()
        assert row["status"] == "cancelled"
        retry = _funnel(tmp_path, tracker, CaptureQueue(), monkeypatch)
        assert retry is ScipDelegationOutcome.ENQUEUED

    def test_an_unreadable_tracker_never_blocks_the_build(self, tmp_path, monkeypatch):
        tracker = ClusterTracker()
        tracker.fail_registration_with = RuntimeError("db down")
        queue = CaptureQueue()

        out = _funnel(tmp_path, tracker, queue, monkeypatch)

        assert out is ScipDelegationOutcome.ENQUEUED
        assert len(queue.builds) == 1

    def test_no_tracker_still_builds(self, tmp_path, monkeypatch):
        queue = CaptureQueue()

        out = _funnel(tmp_path, None, queue, monkeypatch)

        assert out is ScipDelegationOutcome.ENQUEUED


class TestBuildTrackerRegistersOnce:
    def test_a_failed_registration_is_not_retried_on_each_state_change(self, caplog):
        tracker = ClusterTracker()
        tracker.fail_registration_with = RuntimeError("db down")
        build = BuildTracker(tracker, "evolution", "evolution@sha1")

        with caplog.at_level(logging.WARNING):
            build.queued()
            build.on_state("running", None, None)
            build.on_state("failed", "boom", None)

        assert tracker.calls.count("register") == 1
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1

    def test_state_changes_after_a_lost_claim_never_touch_the_winners_row(self):
        tracker = ClusterTracker()
        winner = BuildTracker(tracker, "evolution", "evolution@sha1")
        assert winner.claim().claimed
        loser = BuildTracker(tracker, "evolution", "evolution@sha1")
        claim = loser.claim()
        assert not claim.claimed
        assert claim.blocking_job_id == winner.job_id

        loser.on_state("running", None, None)
        loser.on_state("failed", "boom", None)

        [row] = tracker.jobs.values()
        assert row["status"] == "pending"

    def test_concurrent_claims_have_exactly_one_winner(self):
        tracker = ClusterTracker()
        results: List[bool] = []
        barrier = threading.Barrier(8)

        def claim() -> None:
            barrier.wait()
            results.append(
                BuildTracker(tracker, "evolution", "evolution@sha1").claim().claimed
            )

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert len(tracker.jobs) == 1
