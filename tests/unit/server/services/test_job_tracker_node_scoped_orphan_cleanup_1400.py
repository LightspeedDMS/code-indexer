"""Story #1400 CRITICAL 3: node-scoped orphan cleanup.

BGM's PostgreSQL orphan-cleanup query was unscoped (no node filter), so a
restart of node B could fail a temporal job legitimately still running on
node A. The correct node-scoped primitive
(`cleanup_orphaned_jobs_on_startup(node_id)`) already existed on the backend
but every JobTracker caller invoked it with node_id=None (no-op on PG).
These tests prove JobTracker now threads a real node_id through: at
construction, at cleanup-on-startup, and at job-registration time
(`executing_node` stamped even while pending).
"""

from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.job_tracker import JobTracker


class _FakeJobTrackerBackend:
    """Records calls; mirrors the real backend's public surface used here."""

    def __init__(self) -> None:
        self.cleanup_calls: List[Dict[str, Any]] = []
        self.save_job_calls: List[Dict[str, Any]] = []
        self.atomic_claim_insert_calls: List[Dict[str, Any]] = []

    def cleanup_orphaned_jobs_on_startup(self, node_id: Optional[str] = None) -> int:
        self.cleanup_calls.append({"node_id": node_id})
        return 0

    def save_job(self, **kwargs: Any) -> None:
        self.save_job_calls.append(kwargs)

    def atomic_claim_insert(self, **kwargs: Any) -> None:
        self.atomic_claim_insert_calls.append(kwargs)


class BackgroundJobsPostgresBackend:
    """Real stub whose class name matches the production PG backend, so
    BackgroundJobManager's backend-type detection (by class name) sees a
    genuine PG-shaped type rather than a mock with a spoofed name."""

    def __init__(self) -> None:
        self.fail_orphaned_jobs_called = False

    def fail_orphaned_jobs(self, error: str = "") -> int:
        self.fail_orphaned_jobs_called = True
        return 0


class BackgroundJobsSqliteBackend:
    """Real stub matching the production SQLite backend's class name."""

    def __init__(self) -> None:
        self.fail_orphaned_jobs_called = False

    def fail_orphaned_jobs(self, error: str = "") -> int:
        self.fail_orphaned_jobs_called = True
        return 0


class TestJobTrackerNodeIdConstruction:
    def test_accepts_node_id_param(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "jobs.db")
        tracker = JobTracker(db_path, node_id="node-a")
        assert tracker._node_id == "node-a"

    def test_defaults_node_id_to_none(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "jobs.db")
        tracker = JobTracker(db_path)
        assert tracker._node_id is None


class TestCleanupOrphanedJobsPassesNodeId:
    def test_cleanup_forwards_real_node_id_to_backend(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "jobs.db")
        backend = _FakeJobTrackerBackend()
        tracker = JobTracker(db_path, storage_backend=backend, node_id="node-a")

        tracker.cleanup_orphaned_jobs_on_startup()

        assert backend.cleanup_calls == [{"node_id": "node-a"}]

    def test_cleanup_forwards_none_when_no_node_id_configured(
        self, tmp_path: Any
    ) -> None:
        db_path = str(tmp_path / "jobs.db")
        backend = _FakeJobTrackerBackend()
        tracker = JobTracker(db_path, storage_backend=backend)

        tracker.cleanup_orphaned_jobs_on_startup()

        assert backend.cleanup_calls == [{"node_id": None}]


class TestExecutingNodeStampedAtRegistration:
    def test_register_job_stamps_executing_node(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "jobs.db")
        backend = _FakeJobTrackerBackend()
        tracker = JobTracker(db_path, storage_backend=backend, node_id="node-a")

        tracker.register_job(
            job_id="job-1",
            operation_type="temporal_query",
            username="alice",
            repo_alias="myrepo",
        )

        assert len(backend.save_job_calls) == 1
        assert backend.save_job_calls[0]["executing_node"] == "node-a"

    def test_register_job_if_no_conflict_stamps_executing_node_while_pending(
        self, tmp_path: Any
    ) -> None:
        db_path = str(tmp_path / "jobs.db")
        backend = _FakeJobTrackerBackend()
        tracker = JobTracker(db_path, storage_backend=backend, node_id="node-b")

        tracker.register_job_if_no_conflict(
            job_id="job-2",
            operation_type="temporal_query",
            username="alice",
            repo_alias="myrepo",
        )

        assert len(backend.atomic_claim_insert_calls) == 1
        call = backend.atomic_claim_insert_calls[0]
        assert call["executing_node"] == "node-b"
        # Stamped while the job is still pending, not only once running.
        assert call["status"] == "pending"


class TestBackgroundJobManagerNodeScopedFailOrphaned:
    def test_bgm_accepts_node_id(self) -> None:
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        bgm = BackgroundJobManager(node_id="node-a")
        assert bgm._node_id == "node-a"

    def test_fail_orphaned_jobs_skips_backend_call_for_postgres_backend(self) -> None:
        """The unscoped fail_orphaned_jobs() sweep must never touch the
        cluster-shared PostgreSQL backend -- it would fail every OTHER
        node's in-flight jobs on any single node's restart. Detected by
        backend class name since that's the only cluster-mode signal
        available to BGM without importing the postgres module directly."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        pg_backend = BackgroundJobsPostgresBackend()
        bgm = BackgroundJobManager(storage_backend=pg_backend, node_id="node-a")

        bgm.fail_orphaned_jobs()

        assert pg_backend.fail_orphaned_jobs_called is False

    def test_fail_orphaned_jobs_still_calls_sqlite_backend(self) -> None:
        """Solo/SQLite mode (single process) keeps the pre-#1400 behavior --
        the unscoped sweep is safe there because there is only ever one
        node."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        sqlite_backend = BackgroundJobsSqliteBackend()
        bgm = BackgroundJobManager(storage_backend=sqlite_backend)

        bgm.fail_orphaned_jobs()

        assert sqlite_backend.fail_orphaned_jobs_called is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
