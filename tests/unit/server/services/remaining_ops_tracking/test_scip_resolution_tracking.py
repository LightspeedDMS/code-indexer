"""
AC8: SCIPResolutionQueue job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC8: SCIPResolutionQueue accepts Optional[JobTracker] parameter
- AC8: process_next_project() registers scip_resolution operation type
- AC8: Successful resolution transitions to completed
- AC8: Failed resolution transitions to failed with error
- AC8: Tracker=None doesn't break process_next_project
- AC8: Tracker raising exceptions doesn't break process_next_project
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.scip_resolution_queue import SCIPResolutionQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status="success"):
    """Create a mock resolution response."""
    resp = MagicMock()
    resp.status = status
    return resp


def _make_mock_healing():
    """Create a mock self_healing_service."""
    mock_healing = MagicMock()
    mock_healing.invoke_claude_code = AsyncMock()
    mock_healing.handle_project_response = AsyncMock()
    return mock_healing


# ---------------------------------------------------------------------------
# AC8: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestSCIPResolutionQueueConstructor:
    """AC8: SCIPResolutionQueue accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self):
        """
        SCIPResolutionQueue can be constructed without a job_tracker.

        Given no job_tracker is provided
        When SCIPResolutionQueue is instantiated
        Then no exception is raised and _job_tracker is None
        """
        async def run():
            mock_healing = _make_mock_healing()
            queue = SCIPResolutionQueue(self_healing_service=mock_healing, job_tracker=None)
            assert queue is not None
            assert queue._job_tracker is None

        asyncio.run(run())

    def test_accepts_job_tracker_instance(self, job_tracker):
        """
        SCIPResolutionQueue stores the job_tracker.

        Given a real JobTracker instance
        When SCIPResolutionQueue is instantiated with it
        Then _job_tracker is set
        """
        async def run():
            mock_healing = _make_mock_healing()
            queue = SCIPResolutionQueue(
                self_healing_service=mock_healing, job_tracker=job_tracker
            )
            assert queue._job_tracker is job_tracker

        asyncio.run(run())

    def test_backward_compatible_without_job_tracker(self):
        """
        Existing code that doesn't pass job_tracker still works.

        Given a call without job_tracker parameter
        When SCIPResolutionQueue is instantiated
        Then no TypeError is raised
        """
        async def run():
            mock_healing = MagicMock()
            queue = SCIPResolutionQueue(self_healing_service=mock_healing)
            assert queue is not None

        asyncio.run(run())


# ---------------------------------------------------------------------------
# AC8: scip_resolution job registered during process_next_project
# ---------------------------------------------------------------------------


class TestSCIPResolutionJobRegistration:
    """AC8: scip_resolution operation type is registered during process_next_project."""

    def test_registers_scip_resolution_job(self, job_tracker):
        """
        process_next_project() registers a scip_resolution job.

        Given a SCIPResolutionQueue with job_tracker and a queued project
        When process_next_project() is called
        Then a scip_resolution job exists in the tracker
        """
        mock_healing = _make_mock_healing()
        mock_response = _make_response("success")
        mock_healing.invoke_claude_code.return_value = mock_response

        async def run():
            queue = SCIPResolutionQueue(
                self_healing_service=mock_healing, job_tracker=job_tracker
            )
            await queue.enqueue_project(
                job_id="test-job-001",
                project_path="backend/",
                language="python",
                build_system="poetry",
                stderr="error output",
            )
            await queue.process_next_project()

        asyncio.run(run())

        jobs = job_tracker.query_jobs(operation_type="scip_resolution")
        assert len(jobs) >= 1

    def test_scip_resolution_job_completes_on_success(self, job_tracker):
        """
        scip_resolution job transitions to completed on successful resolution.

        Given a SCIPResolutionQueue with job_tracker
        When process_next_project() succeeds
        Then the scip_resolution job has completed status
        """
        mock_healing = _make_mock_healing()
        mock_response = _make_response("success")
        mock_healing.invoke_claude_code.return_value = mock_response

        async def run():
            queue = SCIPResolutionQueue(
                self_healing_service=mock_healing, job_tracker=job_tracker
            )
            await queue.enqueue_project(
                job_id="test-job-002",
                project_path="frontend/",
                language="typescript",
                build_system="npm",
                stderr="",
            )
            await queue.process_next_project()

        asyncio.run(run())

        jobs = job_tracker.query_jobs(operation_type="scip_resolution", status="completed")
        assert len(jobs) >= 1

    def test_scip_resolution_job_fails_when_exception_raised(self, job_tracker):
        """
        scip_resolution job transitions to failed when invoke_claude_code raises.

        Given a SCIPResolutionQueue with job_tracker
        When invoke_claude_code() raises an exception
        Then a scip_resolution job exists with failed status
        """
        mock_healing = _make_mock_healing()
        mock_healing.invoke_claude_code.side_effect = RuntimeError("Claude timeout")

        async def run():
            queue = SCIPResolutionQueue(
                self_healing_service=mock_healing, job_tracker=job_tracker
            )
            await queue.enqueue_project(
                job_id="test-job-003",
                project_path="service/",
                language="java",
                build_system="maven",
                stderr="compile error",
            )
            await queue.process_next_project()

        asyncio.run(run())

        jobs = job_tracker.query_jobs(operation_type="scip_resolution")
        assert len(jobs) >= 1
        failed = [j for j in jobs if j["status"] == "failed"]
        assert len(failed) >= 1

    def test_no_job_tracker_does_not_break_process_next_project(self):
        """
        When job_tracker is None, process_next_project proceeds normally.

        Given a SCIPResolutionQueue WITHOUT job_tracker
        When process_next_project() is called
        Then no exception is raised
        """
        mock_healing = _make_mock_healing()
        mock_response = _make_response("success")
        mock_healing.invoke_claude_code.return_value = mock_response

        async def run():
            queue = SCIPResolutionQueue(
                self_healing_service=mock_healing, job_tracker=None
            )
            await queue.enqueue_project(
                job_id="test-job-004",
                project_path="lib/",
                language="go",
                build_system="gomod",
                stderr="",
            )
            await queue.process_next_project()

        asyncio.run(run())  # Must not raise

    def test_tracker_exception_does_not_break_process_next_project(self):
        """
        When job_tracker raises on register_job, process_next_project proceeds.

        Given a job_tracker that raises RuntimeError on register_job
        When process_next_project() is called
        Then no exception propagates
        """
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")
        mock_healing = _make_mock_healing()
        mock_response = _make_response("success")
        mock_healing.invoke_claude_code.return_value = mock_response

        async def run():
            queue = SCIPResolutionQueue(
                self_healing_service=mock_healing, job_tracker=broken_tracker
            )
            await queue.enqueue_project(
                job_id="test-job-005",
                project_path="core/",
                language="python",
                build_system="pip",
                stderr="",
            )
            await queue.process_next_project()

        asyncio.run(run())  # Must not raise
