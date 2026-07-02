"""Story #997 - Tests verifying pace_maker_guard is wired into Claude CLI injection points.

Tests that enforce_pace_maker_config() is called by:
1. ClaudeInvoker.invoke()
2. ResearchAssistantService._run_claude_background()
"""

from unittest.mock import MagicMock, patch


class TestClaudeInvokerCallsGuard:
    """Verify ClaudeInvoker.invoke() calls enforce_pace_maker_config."""

    def test_claude_invoker_calls_guard(self) -> None:
        """invoke() must call enforce_pace_maker_config before doing any work."""
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        invoker = ClaudeInvoker()
        guard_calls = []

        def fake_guard() -> None:
            guard_calls.append(True)

        with patch(
            "code_indexer.server.services.claude_invoker.enforce_pace_maker_config",
            side_effect=fake_guard,
        ):
            # Invoke with valid args - will fail at subprocess level but guard must be called
            invoker.invoke(
                flow="test",
                cwd="/tmp",
                prompt="hello",
                timeout=10,
            )

        assert len(guard_calls) == 1, (
            "enforce_pace_maker_config must be called exactly once per invoke()"
        )

    def test_claude_invoker_guard_exception_does_not_prevent_invocation(self) -> None:
        """If enforce_pace_maker_config raises, invoke() must still proceed."""
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        invoker = ClaudeInvoker()

        with patch(
            "code_indexer.server.services.claude_invoker.enforce_pace_maker_config",
            side_effect=RuntimeError("guard blew up"),
        ):
            # Should not raise - invoke must continue despite guard failure
            result = invoker.invoke(
                flow="test",
                cwd="/tmp",
                prompt="hello",
                timeout=10,
            )
        # Result must be an InvocationResult (not an exception)
        assert result is not None


class TestResearchAssistantCallsGuard:
    """Verify ResearchAssistantService._run_claude_background() calls enforce_pace_maker_config."""

    def _make_service(self):
        """Build a minimal ResearchAssistantService with all deps mocked."""
        from code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        mock_db = MagicMock()
        mock_db.get_connection = MagicMock()
        # get_connection returns a context manager
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=MagicMock())
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_db.get_connection.return_value = mock_ctx

        service = ResearchAssistantService.__new__(ResearchAssistantService)
        service._db = mock_db
        service._sessions = {}
        service._default_session = {
            "folder_path": "/tmp",
            "session_id": "test-session",
        }
        service._job_tracker = MagicMock()
        service._job_tracker.update_job = MagicMock()
        service._job_tracker.complete_job = MagicMock()
        service._job_tracker.fail_job = MagicMock()
        service._invoker = MagicMock()
        service._invoker.invoke = MagicMock(
            return_value=MagicMock(success=True, output="ok", error=None)
        )
        return service

    def test_research_assistant_calls_guard(self) -> None:
        """_run_claude_background() must call enforce_pace_maker_config."""
        service = self._make_service()
        guard_calls = []

        def fake_guard() -> None:
            guard_calls.append(True)

        with (
            patch(
                "code_indexer.server.services.research_assistant_service.enforce_pace_maker_config",
                side_effect=fake_guard,
            ),
            patch.object(
                service,
                "get_session",
                return_value={"folder_path": "/tmp", "session_id": "s1"},
            ),
        ):
            # Call _run_claude_background - it will fail somewhere but guard must fire
            try:
                service._run_claude_background(
                    job_id="job1",
                    session_id="s1",
                    claude_prompt="hello",
                    is_first_prompt=True,
                )
            except Exception:
                pass  # We only care that the guard was called, not that invocation succeeds

        assert len(guard_calls) >= 1, (
            "enforce_pace_maker_config must be called at least once in _run_claude_background()"
        )
