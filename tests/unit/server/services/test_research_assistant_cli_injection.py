"""
Unit tests for Bug #472: Research Assistant CLI argument injection.

Problem: When user message text starts with '--' (e.g., '--reconcile'), the
claude CLI parser interprets it as a flag argument rather than prompt content,
causing errors.

The vulnerability exists in the subsequent-message path (and retry path) of
_run_claude_background. The first-message path is NOT vulnerable because it
wraps the user message in guardrails text, so the -p value never starts with '-'.

All 3 subprocess invocation paths use:
    cmd = base_cmd + [..., "-p", claude_prompt]

Fix: Before building the command, sanitize claude_prompt so it never starts
with '-'. The chosen approach is prepending a space: this prevents argparse
from treating the value as a flag while preserving all original content.

Following TDD: Tests written FIRST before implementing the fix.
"""

import time
import tempfile
import shutil
import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.slow
class TestBug472CliInjectionFix:
    """Tests for Bug #472: CLI argument injection via user message text."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os

        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()
        return ResearchAssistantService(db_path=temp_db)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _run_and_capture_calls(self, research_service, message, is_subsequent=False):
        """
        Execute a prompt through the service and return all captured subprocess
        cmd lists (there may be more than one call on the retry path).

        When is_subsequent=True, a prior message pair is inserted to simulate
        a non-first-message invocation.
        """
        session = research_service.create_session()
        session_id = session["id"]

        if is_subsequent:
            research_service.add_message(session_id, "user", "Prior question")
            research_service.add_message(session_id, "assistant", "Prior answer")

        captured_calls = []

        def capture_run(cmd, **kwargs):
            captured_calls.append(list(cmd))
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Claude response"
            mock_result.stderr = ""
            return mock_result

        target = (
            "code_indexer.server.services.research_assistant_service.subprocess.run"
        )
        with patch(target, side_effect=capture_run):
            research_service.execute_prompt(session_id, message)
            time.sleep(0.5)  # wait for background thread

        return captured_calls

    def _extract_prompt_value(self, cmd):
        """Extract the value passed after -p in the cmd list."""
        p_index = cmd.index("-p")
        return cmd[p_index + 1]

    # ------------------------------------------------------------------
    # Tests: first-message path - NOT vulnerable (guardrails wrap user text)
    # ------------------------------------------------------------------

    def test_first_message_prompt_wrapped_in_guardrails(self, research_service):
        """
        Verify that the first-message path wraps user text in guardrails.

        The first message is NOT vulnerable to CLI injection because the user's
        text is appended at the end of a large guardrails preamble, so the -p
        value starts with '## SECURITY CONSTRAINTS...' rather than the user text.

        This test documents the existing behavior as a regression guard.
        """
        message = "--reconcile"
        calls = self._run_and_capture_calls(research_service, message)

        assert len(calls) >= 1, "subprocess.run must have been called"
        cmd = calls[0]
        prompt_value = self._extract_prompt_value(cmd)

        # First message: guardrails prepended, so prompt does NOT start with '-'
        assert not prompt_value.startswith("-"), (
            f"First-message prompt is expected to be wrapped in guardrails. "
            f"If it now starts with '-', the guardrails injection changed. "
            f"Got first 100 chars: {prompt_value[:100]!r}"
        )
        # The user's original text must still be present somewhere in the prompt
        assert "--reconcile" in prompt_value, (
            f"User's original text must appear in the wrapped prompt. "
            f"Got: {prompt_value[-200:]!r}"
        )

    # ------------------------------------------------------------------
    # Tests: subsequent-message path - VULNERABLE (raw user text passed to -p)
    # ------------------------------------------------------------------

    def test_subsequent_message_double_dash_not_treated_as_flag(self, research_service):
        """
        Bug #472: Subsequent message starting with '--' must not inject CLI flags.

        This is the primary bug path. The subsequent-message path passes the raw
        user text directly as the -p argument value. When the text starts with
        '--', argparse/claude CLI interprets it as an unknown flag and raises an
        error.
        """
        message = "--reconcile"
        calls = self._run_and_capture_calls(
            research_service, message, is_subsequent=True
        )

        assert len(calls) >= 1, "subprocess.run must have been called"
        cmd = calls[0]
        prompt_value = self._extract_prompt_value(cmd)

        assert not prompt_value.startswith("-"), (
            f"Subsequent-message prompt passed to -p must not start with '-' "
            f"to prevent CLI flag injection. Got: {prompt_value!r}"
        )

    def test_subsequent_message_single_dash_not_treated_as_flag(self, research_service):
        """
        Bug #472: Subsequent message starting with '-p' must not inject CLI flags.
        """
        message = "-p something"
        calls = self._run_and_capture_calls(
            research_service, message, is_subsequent=True
        )

        assert len(calls) >= 1, "subprocess.run must have been called"
        cmd = calls[0]
        prompt_value = self._extract_prompt_value(cmd)

        assert not prompt_value.startswith("-"), (
            f"Subsequent prompt starting with '-p' must be sanitized. "
            f"Got: {prompt_value!r}"
        )

    def test_subsequent_message_various_double_dash_prefixes(self, research_service):
        """
        Bug #472: Various '--xxx' prefixed subsequent messages must be safe.
        """
        dangerous_messages = [
            "--reconcile",
            "--help",
            "--dangerously-skip-permissions",
            "--model claude-3",
        ]

        for message in dangerous_messages:
            calls = self._run_and_capture_calls(
                research_service, message, is_subsequent=True
            )
            assert len(calls) >= 1, (
                f"subprocess.run must be called for message: {message!r}"
            )
            cmd = calls[0]
            prompt_value = self._extract_prompt_value(cmd)
            assert not prompt_value.startswith("-"), (
                f"Dash-prefixed message must be sanitized before passing to -p. "
                f"Message: {message!r}, got prompt_value: {prompt_value!r}"
            )

    # ------------------------------------------------------------------
    # Tests: content preservation (fix doesn't corrupt the prompt)
    # ------------------------------------------------------------------

    def test_subsequent_dash_prefix_content_preserved(self, research_service):
        """
        Bug #472: After sanitization, original message content must be present.

        The fix must not silently drop the user's text - only prevent the
        leading dash from being misinterpreted as a CLI flag.
        """
        message = "--reconcile is the command I want to know about"
        calls = self._run_and_capture_calls(
            research_service, message, is_subsequent=True
        )

        assert len(calls) >= 1
        cmd = calls[0]
        prompt_value = self._extract_prompt_value(cmd)

        # Original content must be preserved (may have leading whitespace)
        assert "reconcile is the command I want to know about" in prompt_value, (
            f"Original message content must be preserved after sanitization. "
            f"Got: {prompt_value!r}"
        )

    def test_normal_subsequent_message_not_modified(self, research_service):
        """
        Bug #472: Normal subsequent messages (not starting with dashes) must
        be passed through unchanged.

        The fix must be surgical: only affect prompts that start with '-'.
        """
        message = "What is the reconcile command used for?"
        calls = self._run_and_capture_calls(
            research_service, message, is_subsequent=True
        )

        assert len(calls) >= 1, "subprocess.run must have been called"
        cmd = calls[0]
        prompt_value = self._extract_prompt_value(cmd)

        # Normal messages must pass through without modification
        assert prompt_value == message, (
            f"Normal message must not be modified by the fix. "
            f"Expected: {message!r}, got: {prompt_value!r}"
        )

    def test_normal_first_message_not_affected_by_fix(self, research_service):
        """
        Bug #472: Normal first messages must not be affected by the fix.

        The fix must only trigger on dash-prefixed prompts.
        """
        message = "Explain the --reconcile flag please"
        calls = self._run_and_capture_calls(research_service, message)

        assert len(calls) >= 1, "subprocess.run must have been called"
        cmd = calls[0]
        prompt_value = self._extract_prompt_value(cmd)

        # This message starts with 'E', so it must pass through intact
        # (guardrails are prepended but the user text must appear unchanged)
        assert "Explain the --reconcile flag please" in prompt_value, (
            f"Normal first message content must be preserved. Got last 200: "
            f"{prompt_value[-200:]!r}"
        )
