"""
Unit tests for Bug #584: Cross-node job cancellation detection.

Verifies that _check_db_cancellation() polls the SQLite backend for
cancellation triggered on a different cluster node and sets the
in-memory cancelled flag accordingly.
"""

import tempfile
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestCrossNodeCancellation:
    """Bug #584: Cross-node cancellation detection tests."""

    def setup_method(self):
        """Setup test environment with SQLite backend."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_progress_callback_checks_db_cancellation(self):
        """When DB shows job cancelled, in-memory flag must be set to True."""
        job_id = "test-cancel-job"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )
        self.manager.jobs[job_id] = job

        # Mock the sqlite backend to return cancelled status
        mock_backend = MagicMock()
        mock_backend.get_job.return_value = {
            "job_id": job_id,
            "status": "cancelled",
            "cancelled": True,
        }
        self.manager._sqlite_backend = mock_backend

        # Call the method under test
        self.manager._check_db_cancellation(job_id)

        # Verify the in-memory flag was set
        assert self.manager.jobs[job_id].cancelled is True
        mock_backend.get_job.assert_called_once_with(job_id)

    def test_check_db_cancellation_noop_when_no_backend(self):
        """Without a storage backend, _check_db_cancellation must not crash."""
        job_id = "test-no-backend-job"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )
        self.manager.jobs[job_id] = job
        self.manager._sqlite_backend = None

        # Must not raise
        self.manager._check_db_cancellation(job_id)

        # In-memory flag unchanged
        assert self.manager.jobs[job_id].cancelled is False

    def test_check_db_cancellation_noop_when_not_cancelled(self):
        """When DB shows job still running, in-memory flag stays False."""
        job_id = "test-still-running"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )
        self.manager.jobs[job_id] = job

        mock_backend = MagicMock()
        mock_backend.get_job.return_value = {
            "job_id": job_id,
            "status": "running",
            "cancelled": False,
        }
        self.manager._sqlite_backend = mock_backend

        self.manager._check_db_cancellation(job_id)

        assert self.manager.jobs[job_id].cancelled is False

    def test_check_db_cancellation_handles_backend_exception(self):
        """Backend errors must not propagate — best-effort check."""
        job_id = "test-error-job"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )
        self.manager.jobs[job_id] = job

        mock_backend = MagicMock()
        mock_backend.get_job.side_effect = Exception("DB connection lost")
        self.manager._sqlite_backend = mock_backend

        # Must not raise
        self.manager._check_db_cancellation(job_id)

        # In-memory flag unchanged
        assert self.manager.jobs[job_id].cancelled is False

    def test_check_db_cancellation_handles_missing_job_in_db(self):
        """If job not found in DB (get_job returns None), no crash."""
        job_id = "test-missing-db"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )
        self.manager.jobs[job_id] = job

        mock_backend = MagicMock()
        mock_backend.get_job.return_value = None
        self.manager._sqlite_backend = mock_backend

        # Must not raise
        self.manager._check_db_cancellation(job_id)

        assert self.manager.jobs[job_id].cancelled is False
