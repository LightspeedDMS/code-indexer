"""Unit tests for ripgrep/grep exit code handling (Bug #173).

Tests verify that exit code 1 (no matches) logs at DEBUG level,
while exit code 2+ (actual errors) log at WARNING level.

FILE: tests/unit/global_repos/test_regex_search_exit_codes.py
GOAL: Test ripgrep/grep exit code differentiation (Bug #173)
"""

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.server.services.subprocess_executor import (
    ExecutionStatus,
    SearchExecutionResult,
)


@pytest.fixture
def test_repo(tmp_path):
    """Create a test repository structure."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    (repo_path / "test.py").write_text("def func():\n    pass\n")
    return repo_path


@pytest.fixture
def ripgrep_service(test_repo):
    """Create service with ripgrep engine."""
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/rg"
        return RegexSearchService(test_repo)


@pytest.fixture
def grep_service(test_repo):
    """Create service with grep engine."""
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:

        def which_side_effect(cmd):
            return "/usr/bin/grep" if cmd == "grep" else None

        mock_which.side_effect = which_side_effect
        return RegexSearchService(test_repo)


class TestRipgrepExitCodeHandling:
    """Test ripgrep exit code handling and logging levels (Bug #173)."""

    @pytest.mark.asyncio
    async def test_exit_code_0_no_warning_logged(
        self, ripgrep_service, test_repo, caplog
    ):
        """Test exit code 0 (matches found) does not log warning."""
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 0 (success, matches found)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.SUCCESS,
                output_file="/tmp/test.txt",
                exit_code=0,
                timed_out=False,
                stderr_output=None,
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    await ripgrep_service._search_ripgrep(
                        pattern="test",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # No WARNING logs should be present
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_logs) == 0, "Exit code 0 should not log warnings"

    @pytest.mark.asyncio
    async def test_exit_code_1_no_stderr_logs_debug(
        self, ripgrep_service, test_repo, caplog
    ):
        """Test exit code 1 without stderr (no matches) logs at DEBUG level.

        Bug #173: This is the core fix - exit code 1 with no stderr means
        "no matches found" which is normal ripgrep behavior, not an error.
        """
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 1 with no stderr (no matches found)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=1,
                timed_out=False,
                error_message="Command exited with code 1",
                stderr_output=None,  # No stderr = normal "no matches"
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await ripgrep_service._search_ripgrep(
                        pattern="nonexistent_pattern",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 1 should return empty results"

        # Should log at DEBUG level, NOT WARNING
        debug_logs = [r for r in caplog.records if r.levelname == "DEBUG"]
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert len(warning_logs) == 0, "Exit code 1 (no stderr) should NOT log WARNING"
        assert len(debug_logs) > 0, "Exit code 1 (no stderr) should log at DEBUG"
        assert any(
            "no matches" in r.message.lower() for r in debug_logs
        ), "DEBUG log should mention 'no matches'"

    @pytest.mark.asyncio
    async def test_exit_code_1_with_stderr_logs_warning(
        self, ripgrep_service, test_repo, caplog
    ):
        """Test exit code 1 WITH stderr logs at WARNING level.

        Exit code 1 + stderr could indicate a real problem (invalid regex, etc.)
        so it should still be logged as WARNING.
        """
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 1 with stderr (could be invalid regex)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=1,
                timed_out=False,
                error_message="Command exited with code 1",
                stderr_output="regex parse error: invalid escape sequence",
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await ripgrep_service._search_ripgrep(
                        pattern="[invalid",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 1 with stderr should return empty results"

        # Should log at WARNING level (not DEBUG)
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert (
            len(warning_logs) > 0
        ), "Exit code 1 WITH stderr should log at WARNING level"
        assert any(
            "exit code 1" in r.message.lower() for r in warning_logs
        ), "WARNING should mention exit code 1"
        assert any(
            "regex parse error" in r.message.lower() for r in warning_logs
        ), "WARNING should include stderr content"

    @pytest.mark.asyncio
    async def test_exit_code_2_logs_warning(self, ripgrep_service, test_repo, caplog):
        """Test exit code 2+ (actual errors) log at WARNING level."""
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 2 (actual error - permission denied, etc.)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=2,
                timed_out=False,
                error_message="Command exited with code 2",
                stderr_output="permission denied: /restricted/file",
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await ripgrep_service._search_ripgrep(
                        pattern="test",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 2 should return empty results"

        # Should log at WARNING level
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert len(warning_logs) > 0, "Exit code 2 should log at WARNING level"
        assert any(
            "exit code 2" in r.message.lower() for r in warning_logs
        ), "WARNING should mention exit code 2"
        assert any(
            "permission denied" in r.message.lower() for r in warning_logs
        ), "WARNING should include stderr content"

    @pytest.mark.asyncio
    async def test_exit_code_1_empty_stderr_logs_debug(
        self, ripgrep_service, test_repo, caplog
    ):
        """Test exit code 1 with empty string stderr logs at DEBUG level.

        Edge case: stderr_output="" (empty string) should be treated same as None.
        """
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 1 with empty stderr (no matches found)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=1,
                timed_out=False,
                error_message="Command exited with code 1",
                stderr_output="",  # Empty string = no stderr
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await ripgrep_service._search_ripgrep(
                        pattern="nonexistent",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 1 should return empty results"

        # Should log at DEBUG level, NOT WARNING
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert (
            len(warning_logs) == 0
        ), "Exit code 1 (empty stderr) should NOT log WARNING"


class TestGrepExitCodeHandling:
    """Test grep exit code handling and logging levels (Bug #173)."""

    @pytest.mark.asyncio
    async def test_exit_code_0_no_warning_logged(
        self, grep_service, test_repo, caplog
    ):
        """Test exit code 0 (matches found) does not log warning."""
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 0 (success, matches found)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.SUCCESS,
                output_file="/tmp/test.txt",
                exit_code=0,
                timed_out=False,
                stderr_output=None,
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    await grep_service._search_grep(
                        pattern="test",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # No WARNING logs should be present
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_logs) == 0, "Exit code 0 should not log warnings"

    @pytest.mark.asyncio
    async def test_exit_code_1_no_stderr_logs_debug(
        self, grep_service, test_repo, caplog
    ):
        """Test exit code 1 without stderr (no matches) logs at DEBUG level.

        Bug #173: This is the core fix - exit code 1 with no stderr means
        "no matches found" which is normal grep behavior, not an error.
        """
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 1 with no stderr (no matches found)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=1,
                timed_out=False,
                error_message="Command exited with code 1",
                stderr_output=None,  # No stderr = normal "no matches"
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await grep_service._search_grep(
                        pattern="nonexistent_pattern",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 1 should return empty results"

        # Should log at DEBUG level, NOT WARNING
        debug_logs = [r for r in caplog.records if r.levelname == "DEBUG"]
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert len(warning_logs) == 0, "Exit code 1 (no stderr) should NOT log WARNING"
        assert len(debug_logs) > 0, "Exit code 1 (no stderr) should log at DEBUG"
        assert any(
            "no matches" in r.message.lower() for r in debug_logs
        ), "DEBUG log should mention 'no matches'"

    @pytest.mark.asyncio
    async def test_exit_code_1_with_stderr_logs_warning(
        self, grep_service, test_repo, caplog
    ):
        """Test exit code 1 WITH stderr logs at WARNING level."""
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 1 with stderr (could be invalid regex)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=1,
                timed_out=False,
                error_message="Command exited with code 1",
                stderr_output="grep: invalid regex",
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await grep_service._search_grep(
                        pattern="[invalid",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 1 with stderr should return empty results"

        # Should log at WARNING level
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert (
            len(warning_logs) > 0
        ), "Exit code 1 WITH stderr should log at WARNING level"
        assert any(
            "exit code 1" in r.message.lower() for r in warning_logs
        ), "WARNING should mention exit code 1"

    @pytest.mark.asyncio
    async def test_exit_code_2_logs_warning(self, grep_service, test_repo, caplog):
        """Test exit code 2+ (actual errors) log at WARNING level."""
        with patch(
            "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
        ) as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor

            # Mock exit code 2 (actual error)
            mock_result = SearchExecutionResult(
                status=ExecutionStatus.ERROR,
                output_file="/tmp/test.txt",
                exit_code=2,
                timed_out=False,
                error_message="Command exited with code 2",
                stderr_output="grep: /restricted/file: Permission denied",
            )
            mock_executor.execute_with_limits = AsyncMock(return_value=mock_result)
            mock_executor.shutdown = MagicMock()

            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__.return_value.read.return_value = ""

                with caplog.at_level(logging.DEBUG):
                    result = await grep_service._search_grep(
                        pattern="test",
                        search_path=test_repo,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=100,
                        timeout_seconds=10,
                    )

        # Should return empty results
        assert result == ([], 0), "Exit code 2 should return empty results"

        # Should log at WARNING level
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        assert len(warning_logs) > 0, "Exit code 2 should log at WARNING level"
        assert any(
            "exit code 2" in r.message.lower() for r in warning_logs
        ), "WARNING should mention exit code 2"
