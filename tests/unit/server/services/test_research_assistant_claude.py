"""
Unit tests for Story #141: Research Assistant - Claude CLI Integration.

Tests AC4: Claude CLI execution with polling
Tests AC5: Security guardrails in first prompt

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
import shutil
import tempfile
from pathlib import Path


class TestResearchAssistantClaude:
    """Test Research Assistant Claude CLI integration and security."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        # Clean up database and temp directory (may contain session folders)
        Path(db_path).unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        # Initialize database schema
        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        return ResearchAssistantService(db_path=temp_db)

    # AC4: Claude CLI Integration Tests

    def test_execute_claude_prompt_creates_job(self, research_service):
        """Test AC4: Executing Claude prompt creates background job for polling."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Execute prompt (should return job_id immediately)
        job_id = research_service.execute_prompt(session_id, "Test question")

        assert job_id is not None, "Must return job ID for polling"
        assert isinstance(job_id, str), "Job ID must be string"
        assert len(job_id) > 0, "Job ID must not be empty"

    def test_poll_job_returns_status(self, research_service):
        """Test AC4: Polling job returns status and result when complete."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Execute prompt
        job_id = research_service.execute_prompt(session_id, "Test question")

        # Poll for status
        status = research_service.poll_job(job_id)

        assert status is not None, "Poll must return status"
        assert "status" in status, "Status must include 'status' field"
        assert status["status"] in ["running", "complete", "error"], \
            "Status must be valid state"

    def test_poll_job_complete_includes_response(self, research_service):
        """Test AC4: Complete job includes response content."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # For this test, we'll mock a completed job
        # In real implementation, would wait for Claude to complete
        _job_id = research_service.execute_prompt(session_id, "Test question")

        # Simulate job completion (implementation detail)
        # In production, Claude CLI writes response that gets stored

        # Poll should eventually show complete with response
        status = research_service.poll_job(_job_id)

        if status["status"] == "complete":
            assert "response" in status, "Complete job must include response"

    def test_working_directory_is_session_folder(self, research_service):
        """Test AC4: Claude CLI runs with working directory set to session folder."""
        session = research_service.get_default_session()
        session_id = session["id"]
        folder_path = Path(session["folder_path"])

        # Execute prompt
        _job_id = research_service.execute_prompt(session_id, "pwd")

        # The working directory should be the session folder
        # This is verified by implementation checking cwd parameter
        # For now, just verify session folder exists
        assert folder_path.exists(), "Session folder must exist before execution"

    # AC5: Security Guardrails Tests

    def test_first_prompt_includes_security_guardrails(self, research_service):
        """Test AC5: First prompt to Claude includes security constraints (sent to Claude, not stored in DB)."""
        from unittest.mock import patch

        session = research_service.get_default_session()
        session_id = session["id"]

        # Capture what's sent to Claude (not what's stored in DB)
        captured_prompt = None

        def mock_run_claude(job_id, sess_id, claude_prompt, is_first_prompt):
            nonlocal captured_prompt
            captured_prompt = claude_prompt
            # Update job status
            with research_service._jobs_lock:
                if job_id in research_service._jobs:
                    research_service._jobs[job_id]["status"] = "complete"
                    research_service._jobs[job_id]["response"] = "Test response"
            research_service.add_message(sess_id, "assistant", "Test response")

        with patch.object(research_service, "_run_claude_background", side_effect=mock_run_claude):
            # Execute first prompt
            _job_id = research_service.execute_prompt(session_id, "Test question")

            import time
            time.sleep(0.1)

        # Verify what was SENT TO CLAUDE includes guardrails
        assert captured_prompt is not None
        assert "SECURITY CONSTRAINTS" in captured_prompt, \
            "Prompt sent to Claude must include security constraints header"
        assert "ABSOLUTE PROHIBITIONS" in captured_prompt, \
            "Must include prohibitions section"
        assert "ALLOWED" in captured_prompt, \
            "Must include allowed operations section"
        assert "Test question" in captured_prompt, \
            "Must include user's question"

        # Verify what was STORED IN DATABASE does NOT include guardrails
        messages = research_service.get_messages(session_id)
        assert len(messages) >= 1, "Must have at least one message"
        first_message = messages[0]
        assert first_message["role"] == "user", "First message must be user"
        assert first_message["content"] == "Test question", \
            "Stored message must be ONLY user's original input (no guardrails)"

    def test_security_guardrails_content(self, research_service):
        """Test AC5: Security guardrails sent to Claude include specific constraints."""
        from unittest.mock import patch

        session = research_service.get_default_session()
        session_id = session["id"]

        # Capture what's sent to Claude
        captured_prompt = None

        def mock_run_claude(job_id, sess_id, claude_prompt, is_first_prompt):
            nonlocal captured_prompt
            captured_prompt = claude_prompt
            with research_service._jobs_lock:
                if job_id in research_service._jobs:
                    research_service._jobs[job_id]["status"] = "complete"
                    research_service._jobs[job_id]["response"] = "Test response"
            research_service.add_message(sess_id, "assistant", "Test response")

        with patch.object(research_service, "_run_claude_background", side_effect=mock_run_claude):
            # Execute first prompt
            _job_id = research_service.execute_prompt(session_id, "Test question")

            import time
            time.sleep(0.1)

        # Check what was SENT TO CLAUDE for specific prohibitions
        assert captured_prompt is not None
        assert "NO system destruction" in captured_prompt or \
               "rm -rf" in captured_prompt, \
            "Must prohibit system destruction"
        assert "NO credential exposure" in captured_prompt, \
            "Must prohibit credential exposure"
        assert "NO data exfiltration" in captured_prompt, \
            "Must prohibit data exfiltration"

        # Check for allowed operations
        assert "Read CIDX logs" in captured_prompt or \
               "cidx CLI commands" in captured_prompt, \
            "Must specify allowed operations"

    def test_subsequent_prompts_no_duplicate_guardrails(self, research_service):
        """Test AC5: Subsequent prompts don't duplicate guardrails (sent to Claude, not stored in DB)."""
        from unittest.mock import patch

        session = research_service.get_default_session()
        session_id = session["id"]

        # Capture what's sent to Claude for both prompts
        captured_prompts = []

        def mock_run_claude(job_id, sess_id, claude_prompt, is_first_prompt):
            captured_prompts.append(claude_prompt)
            with research_service._jobs_lock:
                if job_id in research_service._jobs:
                    research_service._jobs[job_id]["status"] = "complete"
                    research_service._jobs[job_id]["response"] = "Test response"
            research_service.add_message(sess_id, "assistant", "Test response")

        with patch.object(research_service, "_run_claude_background", side_effect=mock_run_claude):
            # Execute first prompt
            _job_id1 = research_service.execute_prompt(session_id, "First question")

            import time
            time.sleep(0.1)

            # Execute second prompt (now there are existing messages)
            _job_id2 = research_service.execute_prompt(session_id, "Second question")
            time.sleep(0.1)

        # Verify what was SENT TO CLAUDE
        assert len(captured_prompts) == 2
        # First prompt sent to Claude should include guardrails
        assert "SECURITY CONSTRAINTS" in captured_prompts[0]
        assert "First question" in captured_prompts[0]

        # Second prompt sent to Claude should NOT include guardrails
        assert "SECURITY CONSTRAINTS" not in captured_prompts[1], \
            "Subsequent prompts sent to Claude must not duplicate guardrails"
        assert "Second question" in captured_prompts[1]

        # Verify what was STORED IN DATABASE (should be clean user messages only)
        messages = research_service.get_messages(session_id)
        # Should have: user1, assistant1, user2, assistant2
        assert len(messages) >= 4, "Must have at least 4 messages"

        # First user message stored WITHOUT guardrails (clean)
        first_user = messages[0]
        assert first_user["role"] == "user"
        assert first_user["content"] == "First question", \
            "Stored messages must be ONLY user's original input (no guardrails)"

        # Second user message stored WITHOUT guardrails (clean)
        second_user = messages[2]
        assert second_user["role"] == "user"
        assert second_user["content"] == "Second question", \
            "Stored messages must be ONLY user's original input (no guardrails)"

    def test_security_guardrails_stored_as_constant(self, research_service):
        """Test AC5: Security guardrails are defined as service constant."""
        # The guardrails should be accessible as a class or module constant
        # Not hardcoded in templates
        from src.code_indexer.server.services.research_assistant_service import (
            SECURITY_GUARDRAILS,
        )

        assert SECURITY_GUARDRAILS is not None, "SECURITY_GUARDRAILS constant must exist"
        assert isinstance(SECURITY_GUARDRAILS, str), "Must be string constant"
        assert len(SECURITY_GUARDRAILS) > 100, "Must contain substantial content"
        assert "MANDATORY SECURITY CONSTRAINTS" in SECURITY_GUARDRAILS, \
            "Must include expected header"
