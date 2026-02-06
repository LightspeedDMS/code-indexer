"""
Unit tests for Bug #153: Research Assistant Claude Session Retry Logic.

Tests retry logic for Claude CLI session handling:
- First message uses --session-id (creates new session)
- Subsequent message tries --resume first
- Retry logic triggers on "No conversation found" error
- Successful resume doesn't trigger retry

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestResearchAssistantClaudeRetry:
    """Test Research Assistant Claude CLI retry logic for Bug #153."""

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

    def test_first_message_uses_session_id_flag(self, research_service):
        """Test Bug #153: First message uses --session-id to create new session."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Mock subprocess.run to capture the command
        captured_commands = []

        def mock_run(*args, **kwargs):
            captured_commands.append(args[0] if args else kwargs.get('args'))
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Test response"
            result.stderr = ""
            return result

        with patch('subprocess.run', side_effect=mock_run):
            # Execute first prompt
            job_id = research_service.execute_prompt(session_id, "First question")

            # Wait for background thread
            import time
            time.sleep(0.2)

        # Verify command used --session-id (not --resume)
        assert len(captured_commands) == 1, "Should execute Claude CLI once"
        cmd = captured_commands[0]
        assert "--session-id" in cmd, "First message must use --session-id"
        assert "--resume" not in cmd, "First message must NOT use --resume"

    def test_subsequent_message_tries_resume_first(self, research_service):
        """Test Bug #153: Subsequent message tries --resume first."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add a message to make it NOT the first prompt
        research_service.add_message(session_id, "user", "Previous question")
        research_service.add_message(session_id, "assistant", "Previous answer")

        # Mock subprocess.run to capture the command
        captured_commands = []

        def mock_run(*args, **kwargs):
            captured_commands.append(args[0] if args else kwargs.get('args'))
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Test response"
            result.stderr = ""
            return result

        with patch('subprocess.run', side_effect=mock_run):
            # Execute subsequent prompt
            job_id = research_service.execute_prompt(session_id, "Second question")

            # Wait for background thread
            import time
            time.sleep(0.2)

        # Verify command used --resume (not --session-id)
        assert len(captured_commands) == 1, "Should execute Claude CLI once (successful resume)"
        cmd = captured_commands[0]
        assert "--resume" in cmd, "Subsequent message must try --resume first"
        assert "--session-id" not in cmd, "Successful resume should not use --session-id"

    def test_retry_logic_on_no_conversation_found(self, research_service):
        """Test Bug #153: Retry with --session-id when --resume fails with 'No conversation found'."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add a message to make it NOT the first prompt
        research_service.add_message(session_id, "user", "Previous question")
        research_service.add_message(session_id, "assistant", "Previous answer")

        # Mock subprocess.run to fail on --resume, succeed on --session-id
        captured_commands = []
        call_count = [0]

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args')
            captured_commands.append(cmd)
            call_count[0] += 1

            result = MagicMock()

            # First call (--resume) fails with "No conversation found"
            if call_count[0] == 1:
                assert "--resume" in cmd, "First attempt should use --resume"
                result.returncode = 1
                result.stdout = ""
                result.stderr = "Error: No conversation found for session"
                return result

            # Second call (--session-id) succeeds
            elif call_count[0] == 2:
                assert "--session-id" in cmd, "Retry should use --session-id"
                result.returncode = 0
                result.stdout = "Test response"
                result.stderr = ""
                return result

            # Should not reach here
            assert False, "Should only call subprocess.run twice (resume + retry)"

        with patch('subprocess.run', side_effect=mock_run):
            # Execute subsequent prompt
            job_id = research_service.execute_prompt(session_id, "Second question")

            # Wait for background thread
            import time
            time.sleep(0.2)

        # Verify retry logic was triggered
        assert len(captured_commands) == 2, "Should execute Claude CLI twice (resume failed, retry succeeded)"
        assert "--resume" in captured_commands[0], "First attempt must use --resume"
        assert "--session-id" in captured_commands[1], "Retry must use --session-id"

        # Verify job completed successfully
        status = research_service.poll_job(job_id)
        assert status["status"] == "complete", "Job should complete after retry"
        assert status["response"] == "Test response", "Should return response from retry"

    def test_retry_logic_on_not_found_error(self, research_service):
        """Test Bug #153: Retry with --session-id when --resume fails with 'not found' (lowercase)."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add a message to make it NOT the first prompt
        research_service.add_message(session_id, "user", "Previous question")
        research_service.add_message(session_id, "assistant", "Previous answer")

        # Mock subprocess.run to fail on --resume with generic "not found"
        captured_commands = []
        call_count = [0]

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args')
            captured_commands.append(cmd)
            call_count[0] += 1

            result = MagicMock()

            # First call (--resume) fails with "not found"
            if call_count[0] == 1:
                assert "--resume" in cmd, "First attempt should use --resume"
                result.returncode = 1
                result.stdout = ""
                result.stderr = "Session not found"
                return result

            # Second call (--session-id) succeeds
            elif call_count[0] == 2:
                assert "--session-id" in cmd, "Retry should use --session-id"
                result.returncode = 0
                result.stdout = "Test response"
                result.stderr = ""
                return result

            # Should not reach here
            assert False, "Should only call subprocess.run twice"

        with patch('subprocess.run', side_effect=mock_run):
            # Execute subsequent prompt
            job_id = research_service.execute_prompt(session_id, "Second question")

            # Wait for background thread
            import time
            time.sleep(0.2)

        # Verify retry logic was triggered
        assert len(captured_commands) == 2, "Should execute Claude CLI twice"
        assert "--resume" in captured_commands[0], "First attempt must use --resume"
        assert "--session-id" in captured_commands[1], "Retry must use --session-id"

    def test_no_retry_on_successful_resume(self, research_service):
        """Test Bug #153: Successful --resume does NOT trigger retry."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add a message to make it NOT the first prompt
        research_service.add_message(session_id, "user", "Previous question")
        research_service.add_message(session_id, "assistant", "Previous answer")

        # Mock subprocess.run to succeed on --resume
        captured_commands = []

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args')
            captured_commands.append(cmd)

            result = MagicMock()
            result.returncode = 0
            result.stdout = "Test response"
            result.stderr = ""
            return result

        with patch('subprocess.run', side_effect=mock_run):
            # Execute subsequent prompt
            job_id = research_service.execute_prompt(session_id, "Second question")

            # Wait for background thread
            import time
            time.sleep(0.2)

        # Verify NO retry happened (only one call)
        assert len(captured_commands) == 1, "Should execute Claude CLI ONCE (no retry needed)"
        assert "--resume" in captured_commands[0], "Should use --resume"
        assert "--session-id" not in captured_commands[0], "Should NOT use --session-id"

    def test_no_retry_on_other_errors(self, research_service):
        """Test Bug #153: Retry logic does NOT trigger on other errors (only 'not found' errors)."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add a message to make it NOT the first prompt
        research_service.add_message(session_id, "user", "Previous question")
        research_service.add_message(session_id, "assistant", "Previous answer")

        # Mock subprocess.run to fail with a different error
        captured_commands = []

        def mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args')
            captured_commands.append(cmd)

            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "Permission denied"
            return result

        with patch('subprocess.run', side_effect=mock_run):
            # Execute subsequent prompt
            job_id = research_service.execute_prompt(session_id, "Second question")

            # Wait for background thread
            import time
            time.sleep(0.2)

        # Verify NO retry happened (only one call)
        assert len(captured_commands) == 1, "Should execute Claude CLI ONCE (no retry on non-'not found' errors)"
        assert "--resume" in captured_commands[0], "Should use --resume"

        # Verify job failed
        status = research_service.poll_job(job_id)
        assert status["status"] == "error", "Job should fail on non-retryable errors"
        assert "Permission denied" in status["error"], "Should preserve original error"
