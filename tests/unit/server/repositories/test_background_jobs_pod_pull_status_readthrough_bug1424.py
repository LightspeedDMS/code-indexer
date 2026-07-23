"""Cluster-aware read-through for pod-pull jobs (PR #1424 follow-up).

A pod-pull job is left PENDING in the shared background_jobs queue and executed
on WHICHEVER node claims it -- its real progress/status live in the shared DB
row (written cross-node by the claimer's update_progress/complete_job/fail_job).

Bug: submit_job added a node-local in-memory PENDING entry (self.jobs[job_id])
for the pod-pull job and never removed it. Both read paths PREFER the in-memory
copy over the shared DB row (get_job_status returns it first; get_jobs_for_display
adds it to seen_ids and excludes the DB row), so a poll landing on the SUBMITTING
node would show stale PENDING forever -- exactly the cluster-aware-state class of
bug CLAUDE.md prohibits.

Fix: do NOT retain the in-memory entry for a pod-pull job, so both read paths
fall through to the authoritative shared DB row regardless of which node the poll
hits. These tests use a REAL BackgroundJobsSqliteBackend as the shared store and
simulate a different node's writes by updating that shared row directly.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


@pytest.mark.slow
class TestPodPullStatusReadThrough:
    def setup_method(self):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "shared_jobs.db")
        self.storage = os.path.join(self.temp_dir, "jobs.json")
        # Create the real shared background_jobs schema, then a backend over it.
        DatabaseSchema(self.db_path).initialize_database()
        self.shared = BackgroundJobsSqliteBackend(self.db_path)
        self.manager = None

    def teardown_method(self):
        if self.manager is not None:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _bgm(self):
        tracker = MagicMock()
        tracker.register_job_if_no_conflict.return_value = MagicMock()
        self.manager = BackgroundJobManager(
            storage_path=self.storage,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=1
            ),
            storage_backend=self.shared,
            job_tracker=tracker,
            cluster_mode=True,
        )
        # Never actually run a job on the local pool in this test.
        self.manager._execute_job = lambda *a, **k: None
        return self.manager

    def _submit_pod_pull(self, m, metadata):
        return m.submit_job(
            operation_type="add_golden_repo",
            func=lambda progress_callback=None: {"ok": True},
            submitter_username="admin",
            is_admin=True,
            repo_alias="repoA",
            metadata=metadata,
        )

    def _register_shared_row(self, job_id):
        """Represent the PENDING row the JobTracker registered in the shared DB."""
        self.shared.save_job(
            job_id=job_id,
            operation_type="add_golden_repo",
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
            username="admin",
            progress=0,
            repo_alias="repoA",
            is_admin=True,
        )

    def test_get_job_status_reflects_remote_progress_and_completion(self):
        m = self._bgm()
        job_id = self._submit_pod_pull(m, metadata={"alias": "repoA"})
        self._register_shared_row(job_id)

        # A DIFFERENT node claims and reports progress into the shared row.
        self.shared.update_job(
            job_id,
            status="running",
            progress=40,
            current_phase="index",
        )
        status = m.get_job_status(job_id, username="admin", is_admin=True)
        assert status is not None
        assert status["status"] == "running"
        assert status["progress"] == 40

        # ...then completes it in the shared row.
        self.shared.update_job(
            job_id,
            status="completed",
            progress=100,
            result={"success": True},
        )
        status = m.get_job_status(job_id, username="admin", is_admin=True)
        assert status["status"] == "completed"
        assert status["progress"] == 100

    def test_get_jobs_for_display_reflects_shared_db_state(self):
        m = self._bgm()
        job_id = self._submit_pod_pull(m, metadata={"alias": "repoA"})
        self._register_shared_row(job_id)
        self.shared.update_job(job_id, status="running", progress=55)

        jobs, _total, _pages = m.get_jobs_for_display(is_admin=True)
        entry = next((j for j in jobs if j["job_id"] == job_id), None)
        assert entry is not None, "pod-pull job must still be listed (from the DB)"
        assert entry["status"] == "running"
        assert entry["progress"] == 55

    def test_metadata_less_fallback_job_retains_local_entry(self):
        # Scoping guard: without metadata the op falls back to the local pool
        # (not work-stolen), so its in-memory entry MUST be retained.
        m = self._bgm()
        job_id = self._submit_pod_pull(m, metadata=None)
        assert job_id in m.jobs
