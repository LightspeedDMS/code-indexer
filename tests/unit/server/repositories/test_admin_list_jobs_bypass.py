"""
Unit tests for Story #1032: admin bypass in BackgroundJobManager.list_jobs,
get_job_status, and get_jobs_for_display.

AC11: Admin role bypasses per-user filter in /api/jobs and /jobs.
AC5:  Deactivate_repository jobs appear in get_jobs_for_display immediately.

Covered scenarios:
1. list_jobs(is_admin=False) returns ONLY the querying user's own jobs
2. list_jobs(is_admin=True) returns ALL users' jobs (admin bypass)
3. Non-admin User A cannot see User B's jobs (privilege-escalation guard)
4. get_job_status(is_admin=False) returns None for another user's job
5. get_job_status(is_admin=True) returns a job owned by another user
6. get_jobs_for_display(is_admin=True) includes all users' jobs
7. get_jobs_for_display(is_admin=False, username=X) scopes to user X only
8. AC5: A deactivate_repository job appears in get_jobs_for_display immediately
"""

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig

_MAX_CONCURRENT_JOBS = 10
_ADMIN_USERNAME = "admin"
_USER_A = "alice"
_USER_B = "bob"
_DEACTIVATE_OP = "deactivate_repository"
_OTHER_OP = "sync_repository"


def _make_manager(use_sqlite: bool = False, db_path: Optional[str] = None):
    """Create a BackgroundJobManager, optionally backed by SQLite."""
    return BackgroundJobManager(
        use_sqlite=use_sqlite,
        db_path=db_path,
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
        ),
    )


def _inject_job(
    manager: BackgroundJobManager,
    username: str,
    operation_type: str = _DEACTIVATE_OP,
    status: JobStatus = JobStatus.PENDING,
) -> str:
    """Inject a job directly into the manager's in-memory dict."""
    job_id = str(uuid.uuid4())
    job = BackgroundJob(
        job_id=job_id,
        operation_type=operation_type,
        status=status,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        result=None,
        error=None,
        progress=0,
        username=username,
    )
    with manager._lock:
        manager.jobs[job_id] = job
    return job_id


# ---------------------------------------------------------------------------
# AC11 — list_jobs: is_admin bypass (in-memory path)
# ---------------------------------------------------------------------------


class TestListJobsAdminBypassInMemory:
    """Tests for AC11: list_jobs is_admin bypass on the in-memory path."""

    def setup_method(self):
        self.manager = _make_manager()
        # Alice has a deactivate job, Bob has a sync job
        self.alice_job_id = _inject_job(self.manager, _USER_A, _DEACTIVATE_OP)
        self.bob_job_id = _inject_job(self.manager, _USER_B, _OTHER_OP)

    def teardown_method(self):
        self.manager.shutdown()

    def test_non_admin_sees_only_own_jobs(self):
        """is_admin=False (default) returns ONLY the requesting user's jobs."""
        result = self.manager.list_jobs(username=_USER_A, is_admin=False)
        job_ids = [j["job_id"] for j in result["jobs"]]
        assert self.alice_job_id in job_ids, "Alice should see her own job"
        assert self.bob_job_id not in job_ids, "Alice must NOT see Bob's job"

    def test_admin_sees_all_users_jobs(self):
        """is_admin=True returns jobs from ALL users."""
        result = self.manager.list_jobs(username=_ADMIN_USERNAME, is_admin=True)
        job_ids = [j["job_id"] for j in result["jobs"]]
        assert self.alice_job_id in job_ids, "Admin should see Alice's job"
        assert self.bob_job_id in job_ids, "Admin should see Bob's job"

    def test_privilege_escalation_guard_user_a_cannot_see_user_b(self):
        """Regression: User A with is_admin=False cannot see User B's jobs."""
        result = self.manager.list_jobs(username=_USER_A, is_admin=False)
        job_ids = [j["job_id"] for j in result["jobs"]]
        assert self.bob_job_id not in job_ids, (
            "Privilege escalation: User A must NEVER see User B's jobs when is_admin=False"
        )

    def test_default_is_admin_false_preserves_scoping(self):
        """Calling list_jobs without is_admin kwarg defaults to False (no bypass)."""
        result = self.manager.list_jobs(username=_USER_A)
        job_ids = [j["job_id"] for j in result["jobs"]]
        assert self.bob_job_id not in job_ids, (
            "Default is_admin=False must scope to current user only"
        )


# ---------------------------------------------------------------------------
# AC11 — get_job_status: is_admin bypass (in-memory path)
# ---------------------------------------------------------------------------


class TestGetJobStatusAdminBypassInMemory:
    """Tests for AC11: get_job_status is_admin bypass on the in-memory path."""

    def setup_method(self):
        self.manager = _make_manager()
        self.bob_job_id = _inject_job(self.manager, _USER_B, _DEACTIVATE_OP)

    def teardown_method(self):
        self.manager.shutdown()

    def test_admin_can_get_status_of_other_users_job(self):
        """is_admin=True: admin can fetch status of a job owned by another user."""
        result = self.manager.get_job_status(
            self.bob_job_id, _ADMIN_USERNAME, is_admin=True
        )
        assert result is not None, "Admin should be able to get Bob's job status"
        assert result["job_id"] == self.bob_job_id
        assert result["username"] == _USER_B

    def test_non_admin_cannot_get_other_users_job_status(self):
        """is_admin=False: non-admin cannot see another user's job."""
        result = self.manager.get_job_status(
            self.bob_job_id, _ADMIN_USERNAME, is_admin=False
        )
        assert result is None, "Non-admin must NOT see Bob's job status"

    def test_user_can_get_own_job_status(self):
        """is_admin=False: user can always get their own job status."""
        result = self.manager.get_job_status(self.bob_job_id, _USER_B, is_admin=False)
        assert result is not None, "Bob should see his own job"
        assert result["job_id"] == self.bob_job_id


# ---------------------------------------------------------------------------
# AC11 — list_jobs via SQLite backend: is_admin bypass
# ---------------------------------------------------------------------------


class TestListJobsAdminBypassSQLite:
    """Tests for AC11: list_jobs is_admin bypass on the SQLite DB path."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = _make_manager(use_sqlite=True, db_path=self.db_path)

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _insert_db_job(
        self, username: str, operation_type: str = _DEACTIVATE_OP
    ) -> str:
        """Insert a job directly into SQLite (bypasses in-memory dict)."""
        job_id = str(uuid.uuid4())
        self.manager._sqlite_backend.save_job(
            job_id=job_id,
            operation_type=operation_type,
            status="completed",
            username=username,
            created_at=datetime.now(timezone.utc).isoformat(),
            progress=100,
        )
        # Ensure it's NOT in the in-memory dict
        assert job_id not in self.manager.jobs
        return job_id

    def test_non_admin_sees_only_own_jobs_via_db(self):
        """SQLite path: is_admin=False scopes to requesting user only."""
        alice_job = self._insert_db_job(_USER_A, _DEACTIVATE_OP)
        bob_job = self._insert_db_job(_USER_B, _OTHER_OP)

        result = self.manager.list_jobs(username=_USER_A, is_admin=False)
        job_ids = [j["job_id"] for j in result["jobs"]]

        assert alice_job in job_ids, "Alice should see her own job via DB"
        assert bob_job not in job_ids, "Alice must NOT see Bob's job via DB"

    def test_admin_sees_all_users_jobs_via_db(self):
        """SQLite path: is_admin=True returns all users' jobs."""
        alice_job = self._insert_db_job(_USER_A, _DEACTIVATE_OP)
        bob_job = self._insert_db_job(_USER_B, _OTHER_OP)

        result = self.manager.list_jobs(username=_ADMIN_USERNAME, is_admin=True)
        job_ids = [j["job_id"] for j in result["jobs"]]

        assert alice_job in job_ids, "Admin should see Alice's job via DB"
        assert bob_job in job_ids, "Admin should see Bob's job via DB"


# ---------------------------------------------------------------------------
# AC11 — get_job_status via SQLite backend: is_admin bypass
# ---------------------------------------------------------------------------


class TestGetJobStatusAdminBypassSQLite:
    """Tests for AC11: get_job_status is_admin bypass on the SQLite DB path."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = _make_manager(use_sqlite=True, db_path=self.db_path)

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _insert_db_job(self, username: str) -> str:
        """Insert a completed job directly into SQLite."""
        job_id = str(uuid.uuid4())
        self.manager._sqlite_backend.save_job(
            job_id=job_id,
            operation_type=_DEACTIVATE_OP,
            status="completed",
            username=username,
            created_at=datetime.now(timezone.utc).isoformat(),
            progress=100,
        )
        assert job_id not in self.manager.jobs
        return job_id

    def test_admin_can_get_status_of_db_job_owned_by_other_user(self):
        """SQLite path: admin can fetch status of another user's completed job."""
        bob_job = self._insert_db_job(_USER_B)

        result = self.manager.get_job_status(bob_job, _ADMIN_USERNAME, is_admin=True)

        assert result is not None, "Admin should get Bob's DB job status"
        assert result["job_id"] == bob_job

    def test_non_admin_cannot_get_other_users_db_job_status(self):
        """SQLite path: non-admin cannot see another user's completed job."""
        bob_job = self._insert_db_job(_USER_B)

        result = self.manager.get_job_status(bob_job, _ADMIN_USERNAME, is_admin=False)

        assert result is None, "Non-admin must NOT see Bob's DB job status"


# ---------------------------------------------------------------------------
# AC11 — get_jobs_for_display: is_admin bypass
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayAdminBypass:
    """Tests for AC11: get_jobs_for_display is_admin bypass."""

    def setup_method(self):
        self.manager = _make_manager()
        self.alice_job_id = _inject_job(
            self.manager, _USER_A, _DEACTIVATE_OP, JobStatus.RUNNING
        )
        self.bob_job_id = _inject_job(
            self.manager, _USER_B, _OTHER_OP, JobStatus.PENDING
        )

    def teardown_method(self):
        self.manager.shutdown()

    def test_admin_sees_all_active_jobs_in_display(self):
        """is_admin=True: get_jobs_for_display includes all users' active jobs."""
        jobs, total, _pages = self.manager.get_jobs_for_display(is_admin=True)
        job_ids = [j["job_id"] for j in jobs]
        assert self.alice_job_id in job_ids, "Admin display should include Alice's job"
        assert self.bob_job_id in job_ids, "Admin display should include Bob's job"

    def test_non_admin_sees_only_own_jobs_in_display(self):
        """is_admin=False with username: scoped to that user's jobs only."""
        jobs, total, _pages = self.manager.get_jobs_for_display(
            username=_USER_A, is_admin=False
        )
        job_ids = [j["job_id"] for j in jobs]
        assert self.alice_job_id in job_ids, "Alice should see her own job in display"
        assert self.bob_job_id not in job_ids, "Alice must NOT see Bob's job in display"

    def test_default_get_jobs_for_display_shows_all_when_no_username(self):
        """Backward-compat: get_jobs_for_display() with no username/is_admin shows all."""
        jobs, total, _pages = self.manager.get_jobs_for_display()
        job_ids = [j["job_id"] for j in jobs]
        assert self.alice_job_id in job_ids
        assert self.bob_job_id in job_ids


# ---------------------------------------------------------------------------
# AC5 — deactivate_repository job appears in get_jobs_for_display quickly
# ---------------------------------------------------------------------------


class TestDeactivationJobAppearsInDisplay:
    """AC5: A freshly-injected deactivate_repository job appears in display immediately."""

    def setup_method(self):
        self.manager = _make_manager()

    def teardown_method(self):
        self.manager.shutdown()

    def test_deactivation_job_appears_immediately_in_display(self):
        """A PENDING deactivate_repository job is immediately visible in get_jobs_for_display."""
        job_id = _inject_job(self.manager, _USER_A, _DEACTIVATE_OP, JobStatus.PENDING)

        jobs, total, _pages = self.manager.get_jobs_for_display(
            type_filter=_DEACTIVATE_OP
        )
        job_ids = [j["job_id"] for j in jobs]

        assert job_id in job_ids, (
            f"Deactivation job {job_id} must appear in get_jobs_for_display immediately"
        )
        assert total >= 1


# ---------------------------------------------------------------------------
# BLOCKER 1 — count_active_deactivations: cluster-aware (SQLite backend)
# ---------------------------------------------------------------------------


class TestCountActiveDeactivations:
    """Tests for count_active_deactivations cluster-awareness.

    The method must also query the SQLite backend so jobs submitted on other
    cluster nodes (stored in PG/SQLite but not in the local in-memory dict)
    are counted.  De-duplication ensures a job present in both sources is
    counted only once.
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = _make_manager(use_sqlite=True, db_path=self.db_path)

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _insert_db_job(
        self,
        operation_type: str = _DEACTIVATE_OP,
        status: str = "pending",
    ) -> str:
        """Insert a job directly into SQLite (not in in-memory dict)."""
        job_id = str(uuid.uuid4())
        self.manager._sqlite_backend.save_job(
            job_id=job_id,
            operation_type=operation_type,
            status=status,
            username=_USER_A,
            created_at=datetime.now(timezone.utc).isoformat(),
            progress=0,
        )
        assert job_id not in self.manager.jobs
        return job_id

    def test_cluster_pg_only_running_deactivation_is_counted(self):
        """A DB-only running deactivate_repository job must be counted.

        Simulates a deactivation submitted on another cluster node (not in
        this node's in-memory dict but present in the shared DB).
        """
        self._insert_db_job(operation_type=_DEACTIVATE_OP, status="running")

        count = self.manager.count_active_deactivations()

        assert count == 1, (
            "DB-only running deactivation job must be counted by count_active_deactivations"
        )

    def test_cluster_pg_only_pending_deactivation_is_counted(self):
        """A DB-only pending deactivate_repository job must be counted."""
        self._insert_db_job(operation_type=_DEACTIVATE_OP, status="pending")

        count = self.manager.count_active_deactivations()

        assert count == 1

    def test_dedup_job_in_memory_and_db_counted_once(self):
        """A job present in BOTH in-memory dict AND DB must be counted exactly once."""
        # Insert into DB first
        job_id = self._insert_db_job(operation_type=_DEACTIVATE_OP, status="running")
        # Also put the same job into in-memory dict
        job = BackgroundJob(
            job_id=job_id,
            operation_type=_DEACTIVATE_OP,
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username=_USER_A,
        )
        with self.manager._lock:
            self.manager.jobs[job_id] = job

        count = self.manager.count_active_deactivations()

        assert count == 1, (
            "A job in both memory and DB must be counted only once (de-duplication)"
        )

    def test_completed_db_jobs_not_counted(self):
        """Completed deactivate_repository jobs in DB must NOT be counted."""
        self._insert_db_job(operation_type=_DEACTIVATE_OP, status="completed")
        self._insert_db_job(operation_type=_DEACTIVATE_OP, status="failed")

        count = self.manager.count_active_deactivations()

        assert count == 0, "Completed/failed DB deactivations must NOT be counted"

    def test_non_deactivation_db_jobs_not_counted(self):
        """Non-deactivate_repository DB jobs must NOT be counted."""
        self._insert_db_job(operation_type=_OTHER_OP, status="running")

        count = self.manager.count_active_deactivations()

        assert count == 0, "Non-deactivation running jobs must NOT be counted"

    def test_in_memory_plus_db_jobs_summed(self):
        """In-memory AND DB jobs are both counted (no double-counting of distinct jobs)."""
        # One job only in memory
        _inject_job(self.manager, _USER_A, _DEACTIVATE_OP, JobStatus.PENDING)
        # One distinct job only in DB
        self._insert_db_job(operation_type=_DEACTIVATE_OP, status="running")

        count = self.manager.count_active_deactivations()

        assert count == 2, (
            "Both in-memory and DB deactivation jobs must be counted (distinct jobs)"
        )
