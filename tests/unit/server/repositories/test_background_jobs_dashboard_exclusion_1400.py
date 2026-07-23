"""
Tests for Story #1400 Phase 9 (MEDIUM, dashboard-hide fallback gap):

get_recent_jobs_with_filter (the BackgroundJobManager fallback path used
when no JobTracker is configured) previously had NO exclusion mechanism at
all -- unlike the JobTracker path's get_recent_jobs(exclude_operation_types=).
"Mirror xray_search" alone was therefore an incomplete fix; this closes the
gap in the fallback path itself for BOTH its in-memory and SQLite-backed
branches, fixing the pre-existing xray coverage hole as a side effect (per
the locked design's explicit instruction), and adds "temporal_query" as the
new excluded type both paths will need once the temporal job type exists.

TDD: written BEFORE implementation.
"""

import tempfile
import time
from pathlib import Path

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager


def _wait_for_terminal(manager, job_id, timeout=5.0):
    """Poll until the job reaches a terminal status. Bounded loop (Messi #14)."""
    deadline = time.monotonic() + timeout
    terminal = {"completed", "failed", "cancelled"}
    last = None
    while time.monotonic() < deadline:
        d = manager.get_job_status(job_id, username="u1")
        if d is not None:
            last = d.get("status")
            if last in terminal:
                return d
        time.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not reach a terminal status within {timeout}s (last={last})"
    )


class TestGetRecentJobsWithFilterExclusionInMemory:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"
        self.manager = BackgroundJobManager(storage_path=str(self.job_storage_path))

    def teardown_method(self):
        self.manager.shutdown()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_excluded_operation_type_omitted_from_results(self):
        temporal_id = self.manager.submit_job(
            "temporal_query",
            lambda: {"status": "success"},
            submitter_username="u1",
        )
        ordinary_id = self.manager.submit_job(
            "ordinary_op",
            lambda: {"status": "success"},
            submitter_username="u1",
        )
        _wait_for_terminal(self.manager, temporal_id)
        _wait_for_terminal(self.manager, ordinary_id)

        jobs = self.manager.get_recent_jobs_with_filter(
            time_filter="30d",
            limit=20,
            exclude_operation_types=["temporal_query"],
        )
        op_types = {j["operation_type"] for j in jobs}
        assert "temporal_query" not in op_types
        assert "ordinary_op" in op_types

    def test_no_exclusion_list_behaves_unchanged(self):
        """Backward compatibility: omitting exclude_operation_types must not
        filter anything (existing callers don't pass it)."""
        ordinary_id = self.manager.submit_job(
            "ordinary_op",
            lambda: {"status": "success"},
            submitter_username="u1",
        )
        _wait_for_terminal(self.manager, ordinary_id)

        jobs = self.manager.get_recent_jobs_with_filter(time_filter="30d", limit=20)
        op_types = {j["operation_type"] for j in jobs}
        assert "ordinary_op" in op_types


class TestGetRecentJobsWithFilterExclusionSqliteBackend:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "jobs.db")
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        DatabaseSchema(self.db_path).initialize_database()
        self.backend = BackgroundJobsSqliteBackend(self.db_path)
        self.manager = BackgroundJobManager(
            use_sqlite=True, storage_backend=self.backend
        )

    def teardown_method(self):
        self.manager.shutdown()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_excluded_operation_type_omitted_from_sqlite_backed_results(self):
        temporal_id = self.manager.submit_job(
            "temporal_query",
            lambda: {"status": "success"},
            submitter_username="u1",
        )
        ordinary_id = self.manager.submit_job(
            "ordinary_op",
            lambda: {"status": "success"},
            submitter_username="u1",
        )
        _wait_for_terminal(self.manager, temporal_id)
        _wait_for_terminal(self.manager, ordinary_id)

        jobs = self.manager.get_recent_jobs_with_filter(
            time_filter="30d",
            limit=20,
            exclude_operation_types=["temporal_query"],
        )
        op_types = {j["operation_type"] for j in jobs}
        assert "temporal_query" not in op_types
        assert "ordinary_op" in op_types


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
