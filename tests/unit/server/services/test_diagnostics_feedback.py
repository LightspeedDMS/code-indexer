"""
Unit tests for Claude-generated actionable feedback in DiagnosticsService.

Tests Story S7 - Claude-Generated Actionable Feedback:
- AC3: DiagnosticsService has get_actionable_feedback method
- AC4: Feedback method uses Claude CLI execution
- AC5: Error details passed to Claude in prompt
- AC6: Feedback cached for 1 hour
- AC7: Returns None for non-ERROR status
- AC8: No hardcoded troubleshooting messages
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from code_indexer.server.services.diagnostics_service import (
    DiagnosticsService,
    DiagnosticResult,
    DiagnosticStatus,
)


class TestGetActionableFeedback:
    """Tests for get_actionable_feedback method."""

    @pytest.mark.asyncio
    async def test_get_actionable_feedback_returns_none_for_non_error(self):
        """AC7: Should return None for non-ERROR status diagnostics."""
        service = DiagnosticsService()

        # Test WORKING status
        result = DiagnosticResult(
            name="Test Check",
            status=DiagnosticStatus.WORKING,
            message="Everything is fine",
            details={},
        )
        feedback = await service.get_actionable_feedback(result)
        assert feedback is None

        # Test WARNING status
        result_warning = DiagnosticResult(
            name="Test Check",
            status=DiagnosticStatus.WARNING,
            message="Minor issue",
            details={},
        )
        feedback_warning = await service.get_actionable_feedback(result_warning)
        assert feedback_warning is None

        # Test NOT_CONFIGURED status
        result_not_configured = DiagnosticResult(
            name="Test Check",
            status=DiagnosticStatus.NOT_CONFIGURED,
            message="Not set up",
            details={},
        )
        feedback_not_configured = await service.get_actionable_feedback(
            result_not_configured
        )
        assert feedback_not_configured is None

    @pytest.mark.asyncio
    async def test_get_actionable_feedback_calls_claude_for_error(self):
        """AC4: Should invoke Claude CLI for ERROR status diagnostics."""
        service = DiagnosticsService()

        # Mock template loading
        template_content = """You are a CIDX server diagnostics assistant.

DIAGNOSTIC DETAILS:
- Name: {diagnostic_name}
- Status: {diagnostic_status}
- Message: {diagnostic_message}
- Details: {diagnostic_details}

Provide concise troubleshooting steps."""

        error_result = DiagnosticResult(
            name="GitHub API",
            status=DiagnosticStatus.ERROR,
            message="GitHub API request failed: 401",
            details={"status_code": 401},
        )

        claude_response = """The GitHub API token is invalid or expired.

1. Check that your token is correctly configured
2. Generate a new token at https://github.com/settings/tokens
3. Update the token in CIDX server configuration"""

        with patch.object(
            service, "_load_prompt_template", return_value=template_content
        ) as mock_load:
            with patch.object(
                service, "_execute_claude_prompt", return_value=claude_response
            ) as mock_execute:
                feedback = await service.get_actionable_feedback(error_result)

                # Verify template was loaded
                mock_load.assert_called_once_with("diagnostic_troubleshooting.txt")

                # Verify Claude was executed with formatted prompt
                mock_execute.assert_called_once()
                prompt_arg = mock_execute.call_args[0][0]

                # Verify diagnostic details are in prompt (AC5)
                assert "GitHub API" in prompt_arg
                assert "error" in prompt_arg.lower()
                assert "GitHub API request failed: 401" in prompt_arg
                assert "401" in prompt_arg

                # Verify response returned
                assert feedback == claude_response

    @pytest.mark.asyncio
    async def test_get_actionable_feedback_uses_cache_within_ttl(self):
        """AC6: Should use cached feedback within 1-hour TTL."""
        service = DiagnosticsService()

        template_content = "Template: {diagnostic_name}"
        cached_feedback = "Cached troubleshooting guidance"

        error_result = DiagnosticResult(
            name="SSH Keys",
            status=DiagnosticStatus.ERROR,
            message="Permission denied",
            details={},
        )

        with patch.object(
            service, "_load_prompt_template", return_value=template_content
        ):
            with patch.object(
                service, "_execute_claude_prompt", return_value=cached_feedback
            ) as mock_execute:
                # First call - should execute Claude
                feedback1 = await service.get_actionable_feedback(error_result)
                assert feedback1 == cached_feedback
                assert mock_execute.call_count == 1

                # Second call within TTL - should use cache
                feedback2 = await service.get_actionable_feedback(error_result)
                assert feedback2 == cached_feedback
                assert mock_execute.call_count == 1  # Not called again

                # Third call with different error - should execute Claude again
                different_error = DiagnosticResult(
                    name="SSH Keys",
                    status=DiagnosticStatus.ERROR,
                    message="Host key verification failed",
                    details={},
                )
                feedback3 = await service.get_actionable_feedback(different_error)
                assert feedback3 == cached_feedback
                assert mock_execute.call_count == 2  # Called for new error

    @pytest.mark.asyncio
    async def test_get_actionable_feedback_refreshes_cache_after_ttl(self):
        """AC6: Should refresh cached feedback after 1-hour TTL expires."""
        service = DiagnosticsService()

        template_content = "Template: {diagnostic_name}"
        feedback_old = "Old troubleshooting guidance"
        feedback_new = "New troubleshooting guidance"

        error_result = DiagnosticResult(
            name="Database",
            status=DiagnosticStatus.ERROR,
            message="Database locked",
            details={},
        )

        with patch.object(
            service, "_load_prompt_template", return_value=template_content
        ):
            with patch.object(
                service, "_execute_claude_prompt", side_effect=[feedback_old, feedback_new]
            ) as mock_execute:
                # First call
                feedback1 = await service.get_actionable_feedback(error_result)
                assert feedback1 == feedback_old
                assert mock_execute.call_count == 1

                # Manually expire cache by setting timestamp to 2 hours ago
                cache_key = "Database:Database locked"
                assert cache_key in service._feedback_cache
                old_time = datetime.now() - timedelta(hours=2)
                service._feedback_cache[cache_key] = (old_time, feedback_old)

                # Second call after TTL expiry - should execute Claude again
                feedback2 = await service.get_actionable_feedback(error_result)
                assert feedback2 == feedback_new
                assert mock_execute.call_count == 2


class TestLoadPromptTemplate:
    """Tests for _load_prompt_template method."""

    def test_load_prompt_template_reads_file(self):
        """Should read template file from feedback/prompts/ directory."""
        service = DiagnosticsService()

        template_content = """You are a CIDX diagnostics assistant.

Diagnostic: {diagnostic_name}
Status: {diagnostic_status}
Message: {diagnostic_message}"""

        # Mock file reading
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value=template_content):
                result = service._load_prompt_template("diagnostic_troubleshooting.txt")
                assert result == template_content

    def test_load_prompt_template_raises_if_not_found(self):
        """Should raise FileNotFoundError if template doesn't exist."""
        service = DiagnosticsService()

        # Mock file not existing
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError) as exc_info:
                service._load_prompt_template("nonexistent.txt")

            assert "Prompt template not found" in str(exc_info.value)
            assert "nonexistent.txt" in str(exc_info.value)


class TestLoadPromptTemplateIntegration:
    """Integration tests for template loading."""

    def test_load_prompt_template_integration_real_file(self):
        """Integration test: verify actual template file loads correctly."""
        service = DiagnosticsService()
        # This tests real file loading without mocking
        template = service._load_prompt_template("diagnostic_troubleshooting.txt")

        # Verify template contains expected placeholders
        assert "{diagnostic_name}" in template
        assert "{diagnostic_status}" in template
        assert "{diagnostic_message}" in template
        assert "{diagnostic_details}" in template
        assert "troubleshooting" in template.lower()


class TestExecuteClaudePrompt:
    """Tests for _execute_claude_prompt method."""

    @pytest.mark.asyncio
    async def test_execute_claude_prompt_invokes_cli(self):
        """Should execute Claude CLI with provided prompt."""
        service = DiagnosticsService()

        prompt_text = "Test prompt for Claude"
        expected_response = "Claude's troubleshooting guidance"

        # Mock subprocess execution
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(expected_response.encode("utf-8"), b"")
        )
        mock_process.returncode = 0

        async def mock_wait_for(coro, timeout):
            """Mock wait_for that properly awaits coroutines."""
            return await coro

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_process
        ) as mock_create:
            with patch("asyncio.wait_for", side_effect=mock_wait_for):
                response = await service._execute_claude_prompt(prompt_text)

                assert response == expected_response
                mock_create.assert_called_once()
                # Verify claude CLI was invoked
                args = mock_create.call_args[0]
                assert "claude" in args[0] or args[0].endswith("claude")

    @pytest.mark.asyncio
    async def test_execute_claude_prompt_handles_timeout(self):
        """Should handle Claude CLI timeout gracefully."""
        service = DiagnosticsService()

        prompt_text = "Test prompt"

        # Mock timeout
        import asyncio

        with patch("asyncio.create_subprocess_exec"):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                response = await service._execute_claude_prompt(prompt_text)

                # Should return error message, not raise exception
                assert "timeout" in response.lower() or "error" in response.lower()

    @pytest.mark.asyncio
    async def test_execute_claude_prompt_handles_cli_not_found(self):
        """Should handle Claude CLI not installed."""
        service = DiagnosticsService()

        prompt_text = "Test prompt"

        # Mock FileNotFoundError
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            response = await service._execute_claude_prompt(prompt_text)

            # Should return error message
            assert "not found" in response.lower() or "not available" in response.lower()

    @pytest.mark.asyncio
    async def test_execute_claude_prompt_handles_nonzero_exit(self):
        """Should handle Claude CLI non-zero exit code gracefully."""
        service = DiagnosticsService()

        prompt_text = "Test prompt"

        # Mock process with non-zero exit code
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b"Claude CLI error"))
        mock_process.returncode = 1

        async def mock_wait_for(coro, timeout):
            """Mock wait_for that properly awaits coroutines."""
            return await coro

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("asyncio.wait_for", side_effect=mock_wait_for):
                result = await service._execute_claude_prompt(prompt_text)

        # Should return error message, not raise
        assert "error" in result.lower() or "exit code" in result.lower()
