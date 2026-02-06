"""
Unit tests for Bug #153: Research Assistant Resume Fails When Claude Session Missing.

Problem: When sending second message, error "No conversation found with session ID: {uuid}"
occurs because we try --resume with a Claude session ID that doesn't exist in Claude CLI.

Solution: Always use --session-id instead of --resume, since --session-id works for both
creating new sessions and continuing existing ones in Claude CLI.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestBug153ClaudeSessionResume:
    """Test Bug #153: Claude session resume failure fix."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os
        import shutil
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        # Cleanup - remove entire temp directory tree
        shutil.rmtree(temp_dir, ignore_errors=True)

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

    def test_first_message_uses_session_id_flag(self, research_service):
        """
        Test: First message should use --session-id flag.

        This is unchanged behavior - just verifying it still works.
        """
        session = research_service.create_session()
        session_id = session["id"]

        # Mock subprocess.run at the module where it's used
        with patch('src.code_indexer.server.services.research_assistant_service.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Test response",
                stderr=""
            )

            # Send first message (is_first_prompt=True)
            job_id = research_service.execute_prompt(session_id, "First question")

            # Wait for background job to complete
            import time
            time.sleep(0.5)

            # Verify subprocess.run was called
            assert mock_run.called, "subprocess.run must be called"

            # Get the command that was executed
            call_args = mock_run.call_args
            cmd = call_args[0][0]

            # Verify --session-id is used (not --resume)
            assert "--session-id" in cmd, "Must use --session-id for first message"
            assert "--resume" not in cmd, "Must NOT use --resume for first message"

    def test_second_message_uses_session_id_not_resume(self, research_service):
        """
        Test: Second message should use --session-id (NOT --resume).

        This is the BUG FIX: Previously used --resume which fails when Claude CLI
        session doesn't exist. Now should use --session-id which works for both
        new and existing sessions.
        """
        session = research_service.create_session()
        session_id = session["id"]

        # Add first message directly to database (simulating previous conversation)
        research_service.add_message(session_id, "user", "First question")
        research_service.add_message(session_id, "assistant", "First answer")

        # Mock subprocess.run at the module where it's used
        with patch('src.code_indexer.server.services.research_assistant_service.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Second response",
                stderr=""
            )

            # Send second message (is_first_prompt=False because messages exist)
            job_id = research_service.execute_prompt(session_id, "Second question")

            # Wait for background job to complete (increased wait time)
            import time
            time.sleep(0.5)

            # Verify subprocess.run was called
            assert mock_run.called, "subprocess.run must be called"

            # Get the command that was executed
            call_args = mock_run.call_args
            cmd = call_args[0][0]

            # Verify --session-id is used (not --resume)
            assert "--session-id" in cmd, "Must use --session-id for second message"
            assert "--resume" not in cmd, "Must NOT use --resume for second message"

    def test_multiple_messages_always_use_session_id(self, research_service):
        """
        Test: All messages (1st, 2nd, 3rd, etc.) should use --session-id.

        Comprehensive test to ensure --resume is never used regardless of
        message count.
        """
        session = research_service.create_session()
        session_id = session["id"]

        # Mock subprocess.run at the module where it's used
        with patch('src.code_indexer.server.services.research_assistant_service.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Response",
                stderr=""
            )

            # Send 5 messages
            for i in range(5):
                job_id = research_service.execute_prompt(session_id, f"Question {i+1}")

                # Wait for background job to complete
                import time
                time.sleep(0.5)

            # Verify all calls used --session-id (not --resume)
            assert mock_run.call_count == 5, "Must have called subprocess.run 5 times"

            for call in mock_run.call_args_list:
                cmd = call[0][0]
                assert "--session-id" in cmd, f"Call must use --session-id: {cmd}"
                assert "--resume" not in cmd, f"Call must NOT use --resume: {cmd}"

    def test_command_structure_unchanged_except_flag(self, research_service):
        """
        Test: Command structure should be identical except for session-id vs resume.

        Verifies that the fix only changes the flag, not other command elements.
        """
        session = research_service.create_session()
        session_id = session["id"]

        with patch('src.code_indexer.server.services.research_assistant_service.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Response",
                stderr=""
            )

            # Send message
            research_service.execute_prompt(session_id, "Test question")

            # Wait for background job
            import time
            time.sleep(0.5)

            # Get command - subprocess.run is called with positional args
            assert mock_run.called, "subprocess.run must be called"
            call_args = mock_run.call_args
            # call_args[0] is the tuple of positional args, first element is the command list
            cmd = call_args.args[0] if hasattr(call_args, 'args') else call_args[0][0]

            # Verify core command structure
            assert cmd[0] == "claude", "First element must be 'claude'"
            assert "--dangerously-skip-permissions" in cmd, "Must include permissions flag"
            assert "-p" in cmd, "Must include prompt flag"

            # Verify session-id is followed by UUID
            session_id_index = cmd.index("--session-id")
            uuid_value = cmd[session_id_index + 1]

            # Verify it's a valid UUID format
            import uuid
            try:
                uuid.UUID(uuid_value)
            except ValueError:
                pytest.fail(f"Session ID value '{uuid_value}' is not a valid UUID")
