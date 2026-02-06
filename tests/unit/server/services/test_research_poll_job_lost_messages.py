"""
Unit tests for Bug #151: Research Assistant Lost Messages.

Tests the poll_job fallback behavior when job is not found in _jobs dict
but messages exist in the database (e.g., after server restart or job expiry).

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
import tempfile
from pathlib import Path


class TestPollJobDatabaseFallback:
    """Test poll_job fallback to database when job not found in memory."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        Path(db_path).unlink(missing_ok=True)
        Path(temp_dir).rmdir()

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        # Initialize database schema first
        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        return ResearchAssistantService(db_path=temp_db)

    def test_poll_job_not_found_no_messages_returns_error(self, research_service):
        """
        Test: When job not in _jobs dict and no messages in database,
        poll_job should return error status.

        This is the expected behavior when job truly doesn't exist.
        """
        # Create a fake job ID that doesn't exist
        fake_job_id = "00000000-0000-0000-0000-000000000000"

        # Poll for non-existent job
        result = research_service.poll_job(fake_job_id)

        assert result["status"] == "error", "Must return error for non-existent job"
        assert "not found" in result.get("error", "").lower(), \
            "Error message must indicate job not found"

    def test_poll_job_fallback_when_messages_exist(self, research_service):
        """
        Test: When job not in _jobs dict but messages exist in database,
        poll_job should check database and return complete status.

        This simulates the bug scenario: job lost (server restart) but messages
        were successfully stored.
        """
        # Create session and add messages directly to database
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add user message
        research_service.add_message(session_id, "user", "Test question")

        # Add assistant message (simulating Claude CLI completed but job lost)
        research_service.add_message(session_id, "assistant", "Test answer")

        # Create a fake job ID (job was lost, not in _jobs dict)
        fake_job_id = "11111111-1111-1111-1111-111111111111"

        # Poll for the job with session_id (should fallback to checking database)
        result = research_service.poll_job(fake_job_id, session_id=session_id)

        # Should return complete status since messages exist
        assert result["status"] == "complete", \
            "Must return complete when messages exist in database"
        assert result.get("session_id") == session_id, \
            "Must include session_id in response"

    def test_poll_job_fallback_requires_session_id(self, research_service):
        """
        Test: poll_job fallback needs session_id to check database.

        If job not found and no session_id provided, cannot check database.
        Should return error.
        """
        fake_job_id = "33333333-3333-3333-3333-333333333333"

        # Poll without session_id (old behavior)
        result = research_service.poll_job(fake_job_id)

        # Should return error since we can't check database without session_id
        assert result["status"] == "error", \
            "Must return error when job not found and no session_id provided"

    def test_poll_job_fallback_user_message_only_returns_error(self, research_service):
        """
        Test: When job not in _jobs dict and only user message exists,
        poll_job should return error (assistant response never arrived).
        """
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add ONLY user message (simulating Claude still running or crashed)
        research_service.add_message(session_id, "user", "Test question")

        fake_job_id = "22222222-2222-2222-2222-222222222222"
        result = research_service.poll_job(fake_job_id, session_id=session_id)

        assert result["status"] == "error", \
            "Must return error when only user message exists"
        assert "no assistant response" in result.get("error", "").lower()
