"""
Unit tests for Bug #853: admin can cancel 'system' jobs (lifecycle_backfill),
non-admin users cannot cancel other users' jobs, and regular users can still
cancel their own jobs without regression.

TDD Red phase: tests written BEFORE the fix to confirm the bug exists.

Covered scenarios:
1. Admin can cancel a job submitted by "system" user (in-memory path)
2. Non-admin cannot cancel another user's job (in-memory path) — regression guard
3. Admin can cancel any user's job (in-memory path)
4. Regular user can cancel their own job without is_admin (no regression)
5. Default is_admin=False preserves original blocking behavior
6. Admin can cancel a "system" job via DB path (SQLite backend)
7. Non-admin cannot cancel system job via DB path — regression guard
8. User can cancel their own job via DB path (no regression)
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
_SYSTEM_USERNAME = "system"
_ADMIN_USERNAME = "admin"
_OTHER_USERNAME = "alice"
_LIFECYCLE_OP_TYPE = "lifecycle_backfill"
_RUNNING_STATUS = "running"
_LIFECYCLE_JOB_PREFIX = "lifecycle-backfill-"
_TEST_JOB_PREFIX = "test-job-"
_DB_ONLY_MSG = "Test requires job to be DB-only (not in manager.jobs)"


def _make_manager(use_sqlite: bool = False, db_path: Optional[str] = None):
    """Create a BackgroundJobManager, optionally backed by SQLite."""
    return BackgroundJobManager(
        use_sqlite=use_sqlite,
        db_path=db_path,
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
        ),
    )


def _make_job_id(prefix: str) -> str:
    """Generate a unique job ID with the given prefix."""
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _inject_pending_job(manager: BackgroundJobManager, username: str) -> str:
    """Inject a PENDING job directly into the manager's in-memory dict.

    Bypasses the execution queue so the job stays in PENDING state and
    is immediately available for cancellation tests without needing a worker.
    """
    job_id = _make_job_id(_TEST_JOB_PREFIX)
    job = BackgroundJob(
        job_id=job_id,
        operation_type=_LIFECYCLE_OP_TYPE,
        status=JobStatus.PENDING,
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


class TestCancelJobAdminBypassInMemory:
    """Tests for Bug #853 in-memory path: is_admin=True bypasses username check."""

    def setup_method(self):
        self.manager = _make_manager()

    def teardown_method(self):
        self.manager.shutdown()

    def test_admin_can_cancel_system_job_in_memory(self):
        """Bug #853 Fix 1: admin user can cancel a job owned by 'system'."""
        job_id = _inject_pending_job(self.manager, _SYSTEM_USERNAME)

        result = self.manager.cancel_job(job_id, _ADMIN_USERNAME, is_admin=True)

        assert result["success"] is True, (
            f"Admin should be able to cancel system job. Got: {result}"
        )

    def test_non_admin_cannot_cancel_other_users_job_in_memory(self):
        """Regression guard: non-admin cannot cancel another user's job."""
        job_id = _inject_pending_job(self.manager, _SYSTEM_USERNAME)

        result = self.manager.cancel_job(job_id, _ADMIN_USERNAME, is_admin=False)

        assert result["success"] is False, (
            f"Non-admin should NOT cancel a system job. Got: {result}"
        )
        assert "not" in result["message"].lower()

    def test_admin_can_cancel_any_user_job_in_memory(self):
        """Admin bypass: admin with is_admin=True can cancel any user's job."""
        job_id = _inject_pending_job(self.manager, _OTHER_USERNAME)

        result = self.manager.cancel_job(job_id, _ADMIN_USERNAME, is_admin=True)

        assert result["success"] is True, (
            f"Admin should be able to cancel any user's job. Got: {result}"
        )

    def test_regular_user_can_cancel_own_job_in_memory(self):
        """No regression: a user can still cancel their own job without is_admin."""
        job_id = _inject_pending_job(self.manager, _OTHER_USERNAME)

        result = self.manager.cancel_job(job_id, _OTHER_USERNAME, is_admin=False)

        assert result["success"] is True, (
            f"User should be able to cancel their own job. Got: {result}"
        )

    def test_default_is_admin_false_preserves_old_behavior(self):
        """Regression: omitting is_admin defaults to False and blocks cross-user cancel."""
        job_id = _inject_pending_job(self.manager, _SYSTEM_USERNAME)

        result = self.manager.cancel_job(job_id, _ADMIN_USERNAME)  # no is_admin kwarg

        assert result["success"] is False, (
            "Default is_admin=False must preserve original blocking behavior."
        )


class TestCancelJobAdminBypassSQLite:
    """Tests for Bug #853 via SQLite DB path: admin bypasses username check."""

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
        self, job_id: str, username: str, status: str = _RUNNING_STATUS
    ) -> None:
        """Insert a job directly into the SQLite backend to simulate cross-node job."""
        self.manager._sqlite_backend.save_job(
            job_id=job_id,
            operation_type=_LIFECYCLE_OP_TYPE,
            status=status,
            username=username,
            created_at=datetime.now(timezone.utc).isoformat(),
            progress=0,
        )

    def test_admin_can_cancel_system_job_via_db(self):
        """Bug #853 Fix 1 (DB path): admin can cancel system job in SQLite."""
        job_id = _make_job_id(_LIFECYCLE_JOB_PREFIX)
        self._insert_db_job(job_id, _SYSTEM_USERNAME)
        assert job_id not in self.manager.jobs, _DB_ONLY_MSG

        result = self.manager.cancel_job(job_id, _ADMIN_USERNAME, is_admin=True)

        assert result["success"] is True, (
            f"Admin should cancel system job via DB path. Got: {result}"
        )

    def test_non_admin_cannot_cancel_system_job_via_db(self):
        """Regression guard (DB path): non-admin cannot cancel system job."""
        job_id = _make_job_id(_LIFECYCLE_JOB_PREFIX)
        self._insert_db_job(job_id, _SYSTEM_USERNAME)
        assert job_id not in self.manager.jobs, _DB_ONLY_MSG

        result = self.manager.cancel_job(job_id, _ADMIN_USERNAME, is_admin=False)

        assert result["success"] is False, (
            f"Non-admin should NOT cancel system job via DB path. Got: {result}"
        )

    def test_user_can_cancel_own_job_via_db(self):
        """Regression: user cancelling their own job via DB path still works."""
        job_id = _make_job_id(_TEST_JOB_PREFIX)
        self._insert_db_job(job_id, _OTHER_USERNAME)
        assert job_id not in self.manager.jobs, _DB_ONLY_MSG

        result = self.manager.cancel_job(job_id, _OTHER_USERNAME, is_admin=False)

        assert result["success"] is True, (
            f"User should cancel own job via DB path. Got: {result}"
        )
