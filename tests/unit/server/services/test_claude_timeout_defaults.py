"""
Tests verifying Claude CLI timeout defaults are set to production-safe values.

All hardcoded timeout sites must use 1800s (30 min) shell timeout and
1860s (shell + 60s grace) outer timeout.

Sites verified:
  1. LifecycleAnalysisConfig dataclass defaults (config_manager.py)
  2. _DEFAULT_SOFT_TIMEOUT_SECONDS constant (claude_invoker.py)
  3. _CLAUDE_CLI_SOFT_TIMEOUT_SECONDS constant (description_refresh_scheduler.py)
  4. _CLAUDE_CLI_HARD_TIMEOUT_SECONDS constant (description_refresh_scheduler.py)
  5. ClaudeCliManager dispatcher path uses LifecycleAnalysisConfig defaults (not hardcoded 120)
  6. repo_analyzer._extract_info_with_claude calls invoke_claude_cli (not duplicate subprocess)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Named constants — avoids magic number repetition across all test assertions
# ---------------------------------------------------------------------------

EXPECTED_SHELL_TIMEOUT = 1800  # 30 minutes: minimum for large-repo Claude analysis
EXPECTED_OUTER_TIMEOUT = 1860  # shell + 60s SIGKILL grace
LEGACY_SHELL_TIMEOUT = 90  # old hardcoded value being replaced
LEGACY_OUTER_TIMEOUT = 120  # old hardcoded value being replaced


# ---------------------------------------------------------------------------
# Site 1: LifecycleAnalysisConfig defaults
# ---------------------------------------------------------------------------


class TestLifecycleAnalysisConfigDefaults:
    """LifecycleAnalysisConfig must default to EXPECTED_SHELL/OUTER for large-repo support."""

    def test_shell_timeout_default_is_1800(self) -> None:
        """shell_timeout_seconds must default to EXPECTED_SHELL_TIMEOUT (30 minutes)."""
        from code_indexer.server.utils.config_manager import LifecycleAnalysisConfig

        cfg = LifecycleAnalysisConfig()
        assert cfg.shell_timeout_seconds == EXPECTED_SHELL_TIMEOUT, (
            f"Expected shell_timeout_seconds={EXPECTED_SHELL_TIMEOUT}, "
            f"got {cfg.shell_timeout_seconds}. "
            "Large repos require 30-minute timeout for Claude analysis."
        )

    def test_outer_timeout_default_is_1860(self) -> None:
        """outer_timeout_seconds must default to EXPECTED_OUTER_TIMEOUT (shell + 60s grace)."""
        from code_indexer.server.utils.config_manager import LifecycleAnalysisConfig

        cfg = LifecycleAnalysisConfig()
        assert cfg.outer_timeout_seconds == EXPECTED_OUTER_TIMEOUT, (
            f"Expected outer_timeout_seconds={EXPECTED_OUTER_TIMEOUT}, "
            f"got {cfg.outer_timeout_seconds}. "
            "Outer timeout must exceed shell timeout by at least 60s."
        )

    def test_outer_timeout_exceeds_shell_timeout(self) -> None:
        """outer_timeout_seconds must be greater than shell_timeout_seconds."""
        from code_indexer.server.utils.config_manager import LifecycleAnalysisConfig

        cfg = LifecycleAnalysisConfig()
        assert cfg.outer_timeout_seconds > cfg.shell_timeout_seconds, (
            f"outer_timeout_seconds ({cfg.outer_timeout_seconds}) must be greater than "
            f"shell_timeout_seconds ({cfg.shell_timeout_seconds})."
        )


# ---------------------------------------------------------------------------
# Site 2: _DEFAULT_SOFT_TIMEOUT_SECONDS in claude_invoker.py
# ---------------------------------------------------------------------------


class TestClaudeInvokerDefaultTimeout:
    """ClaudeInvoker module constant must default to EXPECTED_SHELL_TIMEOUT."""

    def test_default_soft_timeout_seconds_is_1800(self) -> None:
        """_DEFAULT_SOFT_TIMEOUT_SECONDS must be EXPECTED_SHELL_TIMEOUT (30 minutes)."""
        from code_indexer.server.services import claude_invoker

        assert claude_invoker._DEFAULT_SOFT_TIMEOUT_SECONDS == EXPECTED_SHELL_TIMEOUT, (
            f"Expected _DEFAULT_SOFT_TIMEOUT_SECONDS={EXPECTED_SHELL_TIMEOUT}, "
            f"got {claude_invoker._DEFAULT_SOFT_TIMEOUT_SECONDS}. "
            "This is the fallback when ClaudeInvoker is built without explicit timeout."
        )


# ---------------------------------------------------------------------------
# Sites 3 & 4: Constants in description_refresh_scheduler.py
# ---------------------------------------------------------------------------


class TestDescriptionRefreshSchedulerTimeoutConstants:
    """description_refresh_scheduler module constants must be updated to 1800/1860."""

    def test_soft_timeout_constant_is_1800(self) -> None:
        """_CLAUDE_CLI_SOFT_TIMEOUT_SECONDS must be EXPECTED_SHELL_TIMEOUT (30 minutes)."""
        from code_indexer.server.services import description_refresh_scheduler

        assert (
            description_refresh_scheduler._CLAUDE_CLI_SOFT_TIMEOUT_SECONDS
            == EXPECTED_SHELL_TIMEOUT
        ), (
            f"Expected _CLAUDE_CLI_SOFT_TIMEOUT_SECONDS={EXPECTED_SHELL_TIMEOUT}, "
            f"got {description_refresh_scheduler._CLAUDE_CLI_SOFT_TIMEOUT_SECONDS}."
        )

    def test_hard_timeout_constant_is_1860(self) -> None:
        """_CLAUDE_CLI_HARD_TIMEOUT_SECONDS must be EXPECTED_OUTER_TIMEOUT."""
        from code_indexer.server.services import description_refresh_scheduler

        assert (
            description_refresh_scheduler._CLAUDE_CLI_HARD_TIMEOUT_SECONDS
            == EXPECTED_OUTER_TIMEOUT
        ), (
            f"Expected _CLAUDE_CLI_HARD_TIMEOUT_SECONDS={EXPECTED_OUTER_TIMEOUT}, "
            f"got {description_refresh_scheduler._CLAUDE_CLI_HARD_TIMEOUT_SECONDS}."
        )

    def test_hard_timeout_exceeds_soft_timeout(self) -> None:
        """_CLAUDE_CLI_HARD_TIMEOUT_SECONDS must exceed _CLAUDE_CLI_SOFT_TIMEOUT_SECONDS."""
        from code_indexer.server.services import description_refresh_scheduler

        assert (
            description_refresh_scheduler._CLAUDE_CLI_HARD_TIMEOUT_SECONDS
            > description_refresh_scheduler._CLAUDE_CLI_SOFT_TIMEOUT_SECONDS
        ), "Hard timeout must exceed soft timeout to allow SIGKILL grace period."


# ---------------------------------------------------------------------------
# Site 5: ClaudeCliManager dispatcher path timeout
# ---------------------------------------------------------------------------


class TestClaudeCliManagerDispatcherTimeout:
    """ClaudeCliManager dispatcher path must use LifecycleAnalysisConfig defaults."""

    def test_dispatcher_dispatch_uses_lifecycle_defaults_not_hardcoded_120(
        self,
    ) -> None:
        """
        When _cli_dispatcher is wired, the dispatch() call must use
        LifecycleAnalysisConfig().outer_timeout_seconds (EXPECTED_OUTER_TIMEOUT)
        not the legacy hardcoded LEGACY_OUTER_TIMEOUT.
        """
        from pathlib import Path
        from unittest.mock import MagicMock

        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager
        from code_indexer.server.services.intelligence_cli_invoker import (
            InvocationResult,
        )
        from code_indexer.server.utils.config_manager import LifecycleAnalysisConfig

        expected_timeout = LifecycleAnalysisConfig().outer_timeout_seconds
        assert expected_timeout == EXPECTED_OUTER_TIMEOUT, (
            f"Precondition: expected timeout must be {EXPECTED_OUTER_TIMEOUT}, "
            f"got {expected_timeout}"
        )

        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = InvocationResult(
            success=True,
            output="generated description",
            error="",
            cli_used="claude",
            was_failover=False,
        )

        manager = ClaudeCliManager(
            api_key=None, max_workers=0, cli_dispatcher=mock_dispatcher
        )

        received: list = []

        def callback(success, result):
            received.append((success, result))

        manager._process_work(Path("/fake/repo"), callback)

        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
        actual_timeout = call_kwargs.get("timeout")
        assert actual_timeout == EXPECTED_OUTER_TIMEOUT, (
            f"Expected dispatch(timeout={EXPECTED_OUTER_TIMEOUT}) from "
            f"LifecycleAnalysisConfig defaults, got timeout={actual_timeout}. "
            f"The legacy hardcoded {LEGACY_OUTER_TIMEOUT} must be replaced."
        )


# ---------------------------------------------------------------------------
# Site 6: repo_analyzer._extract_info_with_claude calls invoke_claude_cli
# ---------------------------------------------------------------------------


class TestRepoAnalyzerExtractInfoCallsInvokeClaudeCli:
    """
    _extract_info_with_claude must delegate to invoke_claude_cli instead of
    running a duplicate subprocess with hardcoded timeout LEGACY_SHELL/OUTER_TIMEOUT.
    """

    def test_extract_info_with_claude_delegates_to_invoke_claude_cli(
        self, tmp_path
    ) -> None:
        """
        _extract_info_with_claude must call invoke_claude_cli (the shared
        parameterized wrapper) rather than running subprocess directly for Claude.

        subprocess.run must be called exactly once for the 'which claude' probe
        with the exact signature used by production code. Any additional call
        indicates the old duplicate hardcoded subprocess block is still active.
        """
        from unittest.mock import MagicMock, patch

        from code_indexer.global_repos.repo_analyzer import RepoAnalyzer

        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        (repo_path / "README.md").write_text("# Test Repo\nA test repository.")

        valid_json_output = (
            '{"summary": "A test repo", "technologies": ["python"], '
            '"features": ["feature1"], "use_cases": ["use case 1"], '
            '"purpose": "library"}'
        )

        analyzer = RepoAnalyzer(repo_path=repo_path)

        which_result = MagicMock()
        which_result.returncode = 0

        with (
            patch(
                "code_indexer.global_repos.repo_analyzer.invoke_claude_cli",
                return_value=(True, valid_json_output),
            ) as mock_invoke,
            patch("subprocess.run", return_value=which_result) as mock_subprocess_run,
        ):
            result = analyzer._extract_info_with_claude()

        assert mock_invoke.called, (
            "_extract_info_with_claude must call invoke_claude_cli (the shared wrapper), "
            "not run subprocess directly with hardcoded timeout."
        )

        # Exactly one subprocess.run call for 'which claude'; no second call for Claude itself.
        mock_subprocess_run.assert_called_once_with(
            ["which", "claude"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result is not None, (
            "With valid JSON output from invoke_claude_cli, result must not be None."
        )

    def test_extract_info_with_claude_does_not_hardcode_legacy_timeouts(
        self, tmp_path
    ) -> None:
        """
        The timeouts passed to invoke_claude_cli must come from
        LifecycleAnalysisConfig defaults (EXPECTED_SHELL/OUTER_TIMEOUT),
        not the legacy hardcoded values LEGACY_SHELL/OUTER_TIMEOUT.
        """
        from unittest.mock import MagicMock, patch

        from code_indexer.global_repos.repo_analyzer import RepoAnalyzer
        from code_indexer.server.utils.config_manager import LifecycleAnalysisConfig

        repo_path = tmp_path / "timeout-check-repo"
        repo_path.mkdir()

        defaults = LifecycleAnalysisConfig()
        valid_json_output = (
            '{"summary": "A test repo", "technologies": ["python"], '
            '"features": [], "use_cases": [], "purpose": "library"}'
        )

        analyzer = RepoAnalyzer(repo_path=repo_path)
        which_result = MagicMock()
        which_result.returncode = 0

        captured_args: list = []

        def capture_invoke(
            repo_path_arg, prompt, shell_timeout_seconds, outer_timeout_seconds
        ):
            captured_args.append(
                {
                    "shell": shell_timeout_seconds,
                    "outer": outer_timeout_seconds,
                }
            )
            return (True, valid_json_output)

        with (
            patch(
                "code_indexer.global_repos.repo_analyzer.invoke_claude_cli",
                side_effect=capture_invoke,
            ),
            patch("subprocess.run", return_value=which_result),
        ):
            analyzer._extract_info_with_claude()

        assert len(captured_args) == 1, "invoke_claude_cli must be called exactly once."
        used_shell = captured_args[0]["shell"]
        used_outer = captured_args[0]["outer"]
        assert used_shell == defaults.shell_timeout_seconds, (
            f"shell_timeout_seconds must be {defaults.shell_timeout_seconds} "
            f"(LifecycleAnalysisConfig default), got {used_shell}. "
            f"Legacy hardcoded value was {LEGACY_SHELL_TIMEOUT}."
        )
        assert used_outer == defaults.outer_timeout_seconds, (
            f"outer_timeout_seconds must be {defaults.outer_timeout_seconds} "
            f"(LifecycleAnalysisConfig default), got {used_outer}. "
            f"Legacy hardcoded value was {LEGACY_OUTER_TIMEOUT}."
        )
