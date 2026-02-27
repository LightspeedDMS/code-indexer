"""
AC1: LangfuseTraceSyncService job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC1: LangfuseTraceSyncService accepts Optional[JobTracker] parameter
- AC1: _do_sync_all_projects() registers langfuse_sync operation type
- AC1: Sync completion transitions job to completed
- AC1: Sync failure transitions job to failed with error details
- AC1: Tracker=None doesn't break sync operation
- AC1: Tracker raising exceptions doesn't break sync operation
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.langfuse_trace_sync_service import LangfuseTraceSyncService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(job_tracker=None, config_getter=None):
    """Create a LangfuseTraceSyncService with optional job_tracker."""
    if config_getter is None:
        # Default: disabled langfuse config so _do_sync_all_projects returns early
        from code_indexer.server.utils.config_manager import ServerConfig
        default_config = ServerConfig(server_dir="/tmp")
        config_getter = lambda: default_config

    return LangfuseTraceSyncService(
        config_getter=config_getter,
        data_dir="/tmp",
        job_tracker=job_tracker,
    )


def _make_enabled_config(tmp_path):
    """Create a config with Langfuse pull enabled (minimal)."""
    from code_indexer.server.utils.config_manager import (
        ServerConfig,
        LangfuseConfig,
        LangfusePullProject,
    )
    return ServerConfig(
        server_dir=str(tmp_path),
        langfuse_config=LangfuseConfig(
            pull_enabled=True,
            pull_projects=[LangfusePullProject(public_key="pk", secret_key="sk")],
        ),
    )


# ---------------------------------------------------------------------------
# AC1: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestLangfuseTraceSyncServiceConstructor:
    """AC1: LangfuseTraceSyncService accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self):
        """
        LangfuseTraceSyncService can be constructed without a job_tracker.

        Given no job_tracker is provided
        When LangfuseTraceSyncService is instantiated
        Then no exception is raised and _job_tracker is None
        """
        service = _make_service(job_tracker=None)
        assert service is not None
        assert service._job_tracker is None

    def test_accepts_job_tracker_instance(self, job_tracker):
        """
        LangfuseTraceSyncService stores the job_tracker.

        Given a real JobTracker instance
        When LangfuseTraceSyncService is instantiated with it
        Then _job_tracker is set
        """
        service = _make_service(job_tracker=job_tracker)
        assert service._job_tracker is job_tracker

    def test_backward_compatible_without_job_tracker_parameter(self):
        """
        Existing code that doesn't pass job_tracker still works.

        Given a call without job_tracker parameter
        When LangfuseTraceSyncService is instantiated
        Then no TypeError is raised
        """
        from code_indexer.server.utils.config_manager import ServerConfig
        service = LangfuseTraceSyncService(
            config_getter=lambda: ServerConfig(server_dir="/tmp"),
            data_dir="/tmp",
        )
        assert service is not None


# ---------------------------------------------------------------------------
# AC1: langfuse_sync job registered during _do_sync_all_projects
# ---------------------------------------------------------------------------


class TestLangfuseJobRegistration:
    """AC1: langfuse_sync operation type is registered during sync."""

    def test_registers_langfuse_sync_job_during_sync(self, job_tracker, tmp_path):
        """
        _do_sync_all_projects() registers a langfuse_sync job when sync runs.

        Given a LangfuseTraceSyncService with job_tracker and enabled config
        When _do_sync_all_projects() is called (with mocked actual API call)
        Then a langfuse_sync job exists in the tracker
        """
        config = _make_enabled_config(tmp_path)
        service = _make_service(job_tracker=job_tracker, config_getter=lambda: config)

        # Mock out the actual sync work so it doesn't call Langfuse API
        with patch.object(service, "sync_project"):
            service._do_sync_all_projects()

        # A langfuse_sync job should have been registered (and completed)
        jobs = job_tracker.query_jobs(operation_type="langfuse_sync")
        assert len(jobs) >= 1

    def test_langfuse_sync_job_completes_successfully(self, job_tracker, tmp_path):
        """
        langfuse_sync job transitions to completed after successful sync.

        Given a LangfuseTraceSyncService with job_tracker
        When _do_sync_all_projects() succeeds
        Then the langfuse_sync job is in completed status
        """
        config = _make_enabled_config(tmp_path)
        service = _make_service(job_tracker=job_tracker, config_getter=lambda: config)

        with patch.object(service, "sync_project"):
            service._do_sync_all_projects()

        jobs = job_tracker.query_jobs(operation_type="langfuse_sync", status="completed")
        assert len(jobs) >= 1

    def test_langfuse_sync_job_fails_when_sync_raises(self, job_tracker, tmp_path):
        """
        langfuse_sync job transitions to failed when sync raises an exception.

        Given a LangfuseTraceSyncService with job_tracker
        When sync_project() raises an exception
        Then a langfuse_sync job exists with failed status
        """
        config = _make_enabled_config(tmp_path)
        service = _make_service(job_tracker=job_tracker, config_getter=lambda: config)

        with patch.object(service, "sync_project", side_effect=RuntimeError("API unreachable")):
            service._do_sync_all_projects()

        # Should have a failed job
        jobs = job_tracker.query_jobs(operation_type="langfuse_sync")
        assert len(jobs) >= 1
        # At least one should be failed
        failed = [j for j in jobs if j["status"] == "failed"]
        assert len(failed) >= 1

    def test_no_job_tracker_does_not_break_sync(self, tmp_path):
        """
        When job_tracker is None, sync operation proceeds normally.

        Given a LangfuseTraceSyncService WITHOUT job_tracker
        When _do_sync_all_projects() is called
        Then no exception is raised
        """
        config = _make_enabled_config(tmp_path)
        service = _make_service(job_tracker=None, config_getter=lambda: config)

        # Should not raise
        with patch.object(service, "sync_project"):
            service._do_sync_all_projects()

    def test_tracker_exception_does_not_break_sync(self, tmp_path):
        """
        When job_tracker raises on register_job, sync operation proceeds.

        Given a job_tracker that raises RuntimeError on register_job
        When _do_sync_all_projects() is called
        Then no exception propagates - sync continues
        """
        config = _make_enabled_config(tmp_path)

        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")

        service = _make_service(job_tracker=broken_tracker, config_getter=lambda: config)

        # Should not raise - defensive try/except in service
        with patch.object(service, "sync_project"):
            service._do_sync_all_projects()  # Must not raise


# ---------------------------------------------------------------------------
# AC1: Disabled langfuse config registers no job
# ---------------------------------------------------------------------------


class TestLangfuseDisabledNoJob:
    """AC1: No job registered when Langfuse pull is disabled."""

    def test_no_job_when_langfuse_disabled(self, job_tracker, tmp_path):
        """
        When Langfuse pull is disabled, no langfuse_sync job is registered.

        Given config with langfuse_config.pull_enabled=False
        When _do_sync_all_projects() is called
        Then no langfuse_sync job is registered
        """
        from code_indexer.server.utils.config_manager import ServerConfig
        disabled_config = ServerConfig(server_dir=str(tmp_path))
        service = _make_service(job_tracker=job_tracker, config_getter=lambda: disabled_config)

        service._do_sync_all_projects()

        jobs = job_tracker.query_jobs(operation_type="langfuse_sync")
        assert len(jobs) == 0
