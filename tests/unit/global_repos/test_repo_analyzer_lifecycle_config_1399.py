"""
Bug #1399 CRITICAL item 4 (call site 2/2): RepoAnalyzer._extract_info_with_claude
constructs a fresh, default-valued LifecycleAnalysisConfig() instead of reading
the saved config from ConfigService -- so a Web UI change to
lifecycle_analysis.outer_timeout_seconds / shell_timeout_seconds never reaches
this call site (divergent consumer; the primary LifecycleClaudeCliInvoker path
already reads fresh config correctly -- see
test_lifecycle_claude_cli_invoker_config.py).

Fix: read timeouts via get_config_service().get_config().lifecycle_analysis_config
at call time, mirroring the correct reference implementation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

# Deliberately distinct from LifecycleAnalysisConfig's defaults (1800/1860)
# so a false-pass via defaults is impossible.
_CONFIGURED_SHELL_TIMEOUT_SECONDS = 600
_CONFIGURED_OUTER_TIMEOUT_SECONDS = 650


class TestRepoAnalyzerReadsConfiguredLifecycleTimeout:
    def test_extract_info_with_claude_uses_config_service_timeouts(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.global_repos.repo_analyzer import RepoAnalyzer

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "lifecycle_analysis",
            "shell_timeout_seconds",
            _CONFIGURED_SHELL_TIMEOUT_SECONDS,
        )
        service.update_setting(
            "lifecycle_analysis",
            "outer_timeout_seconds",
            _CONFIGURED_OUTER_TIMEOUT_SECONDS,
        )

        repo_path = tmp_path / "some-repo"
        repo_path.mkdir()

        mock_manager = MagicMock()
        mock_manager.check_cli_available.return_value = True
        mock_manager.sync_api_key.return_value = None

        analyzer = RepoAnalyzer(str(repo_path), claude_cli_manager=mock_manager)

        with (
            patch(
                "code_indexer.global_repos.repo_analyzer.get_config_service",
                return_value=service,
            ),
            patch(
                "code_indexer.global_repos.repo_analyzer.invoke_claude_cli",
                return_value=(True, "summary: test\ntechnologies: []\n"),
            ) as mock_invoke,
        ):
            analyzer._extract_info_with_claude()

        mock_invoke.assert_called_once()
        call_args = mock_invoke.call_args.args
        # invoke_claude_cli(repo_path, prompt, shell_timeout_seconds, outer_timeout_seconds)
        assert call_args[2] == _CONFIGURED_SHELL_TIMEOUT_SECONDS, (
            "Bug #1399: RepoAnalyzer._extract_info_with_claude must read "
            "shell_timeout_seconds from ConfigService at call time, not "
            f"construct a fresh LifecycleAnalysisConfig() default; got "
            f"{call_args[2]!r}."
        )
        assert call_args[3] == _CONFIGURED_OUTER_TIMEOUT_SECONDS, (
            "Bug #1399: RepoAnalyzer._extract_info_with_claude must read "
            "outer_timeout_seconds from ConfigService at call time, not "
            f"construct a fresh LifecycleAnalysisConfig() default; got "
            f"{call_args[3]!r}."
        )
