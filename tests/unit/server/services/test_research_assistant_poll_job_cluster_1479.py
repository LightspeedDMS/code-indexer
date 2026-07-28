"""
Unit tests for Bug #1479: Research Assistant poll_job cluster-aware-state fix.

Root cause: ResearchAssistantService._jobs is a per-process class-level dict.
In a multi-node/multi-worker cluster, a poll_job() call routed to a different
node/worker than the one that ran the job misses the local _jobs dict and
fell straight to a DB-message-only fallback that can only recover a job whose
assistant response is ALREADY persisted -- an in-flight/running job (or a
failed job) was reported as a spurious "Job not found" error.

Fix: on a local _jobs miss, poll_job() now consults the cluster-shared
JobTracker (Story #314 dual-registration, PostgreSQL-backed in cluster mode,
SQLite-backed in solo mode) BEFORE falling back to the message-only DB check.

Cluster simulation fidelity: two independent JobTracker instances are created
against the SAME on-disk SQLite database, mirroring two different node
processes sharing one cluster-wide backing store. Each JobTracker keeps its
OWN in-memory `_active_jobs` cache (instance-level), so a job registered via
`tracker_a` is genuinely NOT in `tracker_b`'s memory -- `tracker_b.get_job()`
must perform a real backend round trip, exactly like a poll landing on a
different node in production. This is a real JobTracker + real SQLite (per
this project's faithful-DB-mock standard), never a mock of the tracker
itself.

Following TDD methodology: RED tests written first against the pre-fix
behavior, then production code changed to turn them GREEN.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database file (research + background_jobs schema)."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "test.db")

    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()

    yield db_path

    Path(db_path).unlink(missing_ok=True)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _make_service(db_path: str, job_tracker) -> ResearchAssistantService:
    """Create a ResearchAssistantService bound to db_path with the given tracker."""
    return ResearchAssistantService(db_path=db_path, job_tracker=job_tracker)


class TestPollJobCrossNodeRunning:
    """A job running on 'node A' must poll as running from 'node B', not error."""

    def test_cross_node_poll_of_running_job_returns_running_not_error(self, temp_db):
        """
        Given a job registered+running in the shared JobTracker (node A)
        And the polling service instance's local _jobs dict has NO entry for it
            (simulating a poll routed to a different node/worker, node B)
        When poll_job() is called (without an explicit session_id, forcing
            resolution entirely through the tracker's stored metadata)
        Then the result must be status="running", never the spurious
            "Job not found" error.
        """
        tracker_a = JobTracker(temp_db)  # "node A" -- runs the job
        service_a = _make_service(temp_db, tracker_a)

        session = service_a.get_default_session()
        session_id = session["id"]
        service_a.add_message(session_id, "user", "What does this repo do?")

        job_id = str(uuid.uuid4())
        tracker_a.register_job(
            job_id,
            "research_assistant_chat",
            username="system",
            repo_alias="server",
            metadata={"session_id": session_id},
        )
        tracker_a.update_status(job_id, status="running")

        # This job_id was NEVER written into the class-level _jobs dict by
        # this test -- confirm the precondition that mimics a cross-node miss.
        with service_a._jobs_lock:
            assert job_id not in service_a._jobs

        # "node B": a fresh JobTracker instance against the SAME db file --
        # its own in-memory _active_jobs cache is empty, forcing a real
        # backend read for job_id (faithful cross-node simulation).
        tracker_b = JobTracker(temp_db)
        service_b = _make_service(temp_db, tracker_b)

        result = service_b.poll_job(job_id)

        assert result["status"] == "running", (
            f"Cross-node poll of an in-flight job must report running, got: {result}"
        )


class TestPollJobCrossNodeComplete:
    """A job completed on 'node A' must poll as complete with its response from 'node B'."""

    def test_cross_node_poll_of_completed_job_returns_response(self, temp_db):
        """
        Given a job marked complete in the shared JobTracker (node A)
        And its assistant response already persisted to the messages DB
        And the polling service instance's local _jobs dict has NO entry
        When poll_job() is called without an explicit session_id
        Then the result must be status="complete" with the persisted response.
        """
        tracker_a = JobTracker(temp_db)
        service_a = _make_service(temp_db, tracker_a)

        session = service_a.get_default_session()
        session_id = session["id"]
        service_a.add_message(session_id, "user", "Summarize the auth module")
        service_a.add_message(session_id, "assistant", "The auth module does X.")

        job_id = str(uuid.uuid4())
        tracker_a.register_job(
            job_id,
            "research_assistant_chat",
            username="system",
            repo_alias="server",
            metadata={"session_id": session_id},
        )
        tracker_a.update_status(job_id, status="running")
        tracker_a.complete_job(job_id)

        with service_a._jobs_lock:
            assert job_id not in service_a._jobs

        tracker_b = JobTracker(temp_db)
        service_b = _make_service(temp_db, tracker_b)

        result = service_b.poll_job(job_id)

        assert result["status"] == "complete"
        assert result["response"] == "The auth module does X."
        assert result["session_id"] == session_id


class TestPollJobCrossNodeFailed:
    """A job failed on 'node A' must poll as error (with the real error) from 'node B'."""

    def test_cross_node_poll_of_failed_job_returns_tracker_error(self, temp_db):
        """
        Given a job marked failed in the shared JobTracker (node A), with no
            assistant response ever persisted (it failed before completing)
        And the polling service instance's local _jobs dict has NO entry
        When poll_job() is called without an explicit session_id
        Then the result must be status="error" carrying the tracker's error
            message (not the generic "Job not found" message).
        """
        tracker_a = JobTracker(temp_db)
        service_a = _make_service(temp_db, tracker_a)

        session = service_a.get_default_session()
        session_id = session["id"]
        service_a.add_message(session_id, "user", "Do something impossible")

        job_id = str(uuid.uuid4())
        tracker_a.register_job(
            job_id,
            "research_assistant_chat",
            username="system",
            repo_alias="server",
            metadata={"session_id": session_id},
        )
        tracker_a.update_status(job_id, status="running")
        tracker_a.fail_job(job_id, error="Claude CLI execution timed out")

        with service_a._jobs_lock:
            assert job_id not in service_a._jobs

        tracker_b = JobTracker(temp_db)
        service_b = _make_service(temp_db, tracker_b)

        result = service_b.poll_job(job_id)

        assert result["status"] == "error"
        assert result["error"] == "Claude CLI execution timed out"


class TestPollJobSameNodeFastPathUnchanged:
    """The existing same-node _jobs-dict hit path must remain unchanged."""

    def test_same_node_jobs_dict_hit_is_authoritative(self, temp_db):
        """
        Given a job present in the LOCAL _jobs dict (same-node poll)
        When poll_job() is called
        Then the local _jobs entry is returned directly, without needing to
            consult the JobTracker at all.
        """
        tracker = JobTracker(temp_db)
        service = _make_service(temp_db, tracker)

        session = service.get_default_session()
        session_id = session["id"]

        job_id = str(uuid.uuid4())
        with service._jobs_lock:
            service._jobs[job_id] = {
                "status": "running",
                "session_id": session_id,
                "user_prompt": "hi",
                "response": None,
                "error": None,
            }

        try:
            result = service.poll_job(job_id)
            assert result["status"] == "running"
            assert result["session_id"] == session_id
        finally:
            with service._jobs_lock:
                service._jobs.pop(job_id, None)


class TestPollJobGenuineNotFoundUnchanged:
    """A job unknown to _jobs, the tracker, AND the message DB is still 'Job not found'."""

    def test_unknown_job_with_no_tracker_record_returns_not_found(self, temp_db):
        """
        Given a job_id that was never registered anywhere (no _jobs entry, no
            JobTracker record, no session/messages)
        When poll_job() is called with no session_id
        Then the result is the genuine "Job not found" error (not a crash,
            not a false "running"/"complete").
        """
        tracker = JobTracker(temp_db)
        service = _make_service(temp_db, tracker)

        bogus_job_id = str(uuid.uuid4())

        result = service.poll_job(bogus_job_id)

        assert result == {"status": "error", "error": "Job not found"}
