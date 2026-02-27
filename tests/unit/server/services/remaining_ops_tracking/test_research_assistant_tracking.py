"""
AC3, AC4: ResearchAssistantService job_tracker integration.

Story #314 - Epic #261 Unified Job Tracking Subsystem.

Tests:
- AC3: ResearchAssistantService accepts Optional[JobTracker] parameter
- AC3: execute_prompt() registers research_assistant_chat operation type
- AC4: Dual tracking - existing _jobs dict + JobTracker both updated
- AC3: Tracker=None doesn't break execute_prompt
- AC3: Tracker raising exceptions doesn't break execute_prompt
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.research_assistant_service import ResearchAssistantService
from code_indexer.server.storage.database_manager import DatabaseSchema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(db_path, job_tracker=None):
    """Create a ResearchAssistantService with initialized DB schema and optional job_tracker."""
    # Initialize DB schema first so tables like research_sessions exist
    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()
    return ResearchAssistantService(db_path=db_path, job_tracker=job_tracker)


# ---------------------------------------------------------------------------
# AC3: Constructor accepts Optional[JobTracker]
# ---------------------------------------------------------------------------


class TestResearchAssistantServiceConstructor:
    """AC3: ResearchAssistantService accepts Optional[JobTracker] parameter."""

    def test_accepts_none_job_tracker(self, tmp_path):
        """
        ResearchAssistantService can be constructed without a job_tracker.

        Given no job_tracker is provided
        When ResearchAssistantService is instantiated
        Then no exception is raised and _job_tracker is None
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=None)
        assert service is not None
        assert service._job_tracker is None

    def test_accepts_job_tracker_instance(self, tmp_path, job_tracker):
        """
        ResearchAssistantService stores the job_tracker.

        Given a real JobTracker instance
        When ResearchAssistantService is instantiated with it
        Then _job_tracker is set
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=job_tracker)
        assert service._job_tracker is job_tracker

    def test_backward_compatible_without_job_tracker_parameter(self, tmp_path):
        """
        Existing code that doesn't pass job_tracker still works.

        Given a call without job_tracker parameter
        When ResearchAssistantService is instantiated
        Then no TypeError is raised
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db)
        assert service is not None


# ---------------------------------------------------------------------------
# AC3: research_assistant_chat job registered during execute_prompt
# ---------------------------------------------------------------------------


class TestResearchAssistantJobRegistration:
    """AC3: research_assistant_chat operation type is registered during execute_prompt."""

    def _setup_session(self, service):
        """Create a default session for testing."""
        session = service.get_default_session()
        return session["id"]

    def test_registers_research_assistant_chat_job(self, tmp_path, job_tracker):
        """
        execute_prompt() registers a research_assistant_chat job.

        Given a ResearchAssistantService with job_tracker
        When execute_prompt() is called (with mocked Claude execution)
        Then a research_assistant_chat job exists in the tracker
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=job_tracker)
        session_id = self._setup_session(service)

        # Mock the background thread execution so it completes instantly
        with patch.object(service, "_run_claude_background"):
            service.execute_prompt(session_id, "What is the status?")

        # A research_assistant_chat job should have been registered
        jobs = job_tracker.query_jobs(operation_type="research_assistant_chat")
        assert len(jobs) >= 1

    def test_job_id_matches_in_both_tracking_systems(self, tmp_path, job_tracker):
        """
        AC4: The same job_id appears in both _jobs dict and JobTracker.

        Given a ResearchAssistantService with job_tracker
        When execute_prompt() is called
        Then the returned job_id exists in both _jobs dict and JobTracker
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=job_tracker)
        session_id = self._setup_session(service)

        with patch.object(service, "_run_claude_background"):
            returned_job_id = service.execute_prompt(session_id, "Test prompt")

        # Check _jobs dict (existing tracking)
        with service._jobs_lock:
            assert returned_job_id in service._jobs

        # Check JobTracker (new tracking)
        tracked_job = job_tracker.get_job(returned_job_id)
        assert tracked_job is not None

    def test_no_job_tracker_does_not_break_execute_prompt(self, tmp_path):
        """
        When job_tracker is None, execute_prompt proceeds normally.

        Given a ResearchAssistantService WITHOUT job_tracker
        When execute_prompt() is called
        Then no exception is raised and job_id is returned
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=None)
        session_id = self._setup_session(service)

        with patch.object(service, "_run_claude_background"):
            job_id = service.execute_prompt(session_id, "Test prompt")

        assert job_id is not None
        assert len(job_id) > 0

    def test_tracker_exception_does_not_break_execute_prompt(self, tmp_path):
        """
        When job_tracker raises on register_job, execute_prompt proceeds.

        Given a job_tracker that raises RuntimeError on register_job
        When execute_prompt() is called
        Then no exception propagates - execution continues normally
        """
        db = str(tmp_path / "ra.db")
        broken_tracker = MagicMock(spec=JobTracker)
        broken_tracker.register_job.side_effect = RuntimeError("DB unavailable")
        service = _make_service(db_path=db, job_tracker=broken_tracker)
        session_id = self._setup_session(service)

        with patch.object(service, "_run_claude_background"):
            job_id = service.execute_prompt(session_id, "Test prompt")

        # Must return a valid job_id despite tracker failure
        assert job_id is not None


# ---------------------------------------------------------------------------
# AC4: Dual tracking integrity
# ---------------------------------------------------------------------------


class TestDualTrackingIntegrity:
    """AC4: _jobs dict tracking and JobTracker tracking are both maintained."""

    def _setup_session(self, service):
        """Create a default session for testing."""
        session = service.get_default_session()
        return session["id"]

    def test_existing_jobs_dict_still_works_for_polling(self, tmp_path, job_tracker):
        """
        AC4: After execute_prompt, poll_job() still uses _jobs dict as before.

        Given a ResearchAssistantService with job_tracker
        When execute_prompt() is called
        Then poll_job() can retrieve job status from _jobs dict
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=job_tracker)
        session_id = self._setup_session(service)

        with patch.object(service, "_run_claude_background"):
            job_id = service.execute_prompt(session_id, "Test prompt")

        # poll_job should still work (uses _jobs dict)
        poll_result = service.poll_job(job_id, session_id=session_id)
        assert poll_result is not None
        assert "status" in poll_result

    def test_job_tracker_has_correct_operation_type(self, tmp_path, job_tracker):
        """
        AC4: JobTracker records research_assistant_chat as operation_type.

        Given a ResearchAssistantService with job_tracker
        When execute_prompt() is called
        Then the job in JobTracker has operation_type='research_assistant_chat'
        """
        db = str(tmp_path / "ra.db")
        service = _make_service(db_path=db, job_tracker=job_tracker)
        session_id = self._setup_session(service)

        with patch.object(service, "_run_claude_background"):
            job_id = service.execute_prompt(session_id, "Test prompt")

        tracked_job = job_tracker.get_job(job_id)
        assert tracked_job is not None
        assert tracked_job.operation_type == "research_assistant_chat"
