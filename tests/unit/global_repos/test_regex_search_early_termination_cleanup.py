"""Unit tests for Issue #1601 AC-A6/4b -- temp-file cleanup on the
early-termination path is independent of, and does not depend on, the
subprocess-termination mechanism itself.

Fix direction 4b requires the temp file to be reliably removed on the
early-termination path exactly as it already is on the normal-completion
and error paths -- tested here independently of AC-A6's proof (in
tests/unit/server/services/test_subprocess_executor_output_cap.py) that
the subprocess itself gets killed. A fix that kills the subprocess but
leaks the temp file (or vice versa) does not satisfy the issue.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_TEST_MAX_RESULTS = 100
_TEST_TIMEOUT_SECONDS = 10


def _mock_output_capped_executor(content: str, captured_path: dict):
    """Build a mocked SubprocessExecutor reporting an early-termination
    (output_capped=True) completion, having written ``content`` into the
    real output_file_path it is given (and recorded into
    ``captured_path["path"]`` for the test to assert against after the
    call returns) -- simulating what SubprocessExecutor itself leaves
    behind when it kills a still-running, size-capped subprocess."""

    async def _side_effect(**kwargs):
        captured_path["path"] = kwargs["output_file_path"]
        with open(kwargs["output_file_path"], "w") as f:
            f.write(content)
        result = MagicMock()
        result.timed_out = False
        result.status = "success"
        result.exit_code = -9
        result.stderr_output = None
        result.output_capped = True
        return result

    mock_executor = MagicMock()
    mock_executor.execute_with_limits = AsyncMock(side_effect=_side_effect)
    return mock_executor


class TestEarlyTerminationCleanup:
    """AC-A6/4b: the temp file is removed on the early-termination path,
    tested independently from proof that the subprocess itself is killed."""

    @pytest.mark.asyncio
    async def test_ripgrep_temp_file_removed_when_output_capped(self, tmp_path):
        """_search_ripgrep must remove its temp file even when the executor
        reports output_capped=True (the subprocess was killed mid-write)."""
        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            service = RegexSearchService(tmp_path)

        captured_path: dict = {}
        # Content is irrelevant to this test (it only proves cleanup +
        # read_capped propagation, not parse correctness) -- empty avoids
        # needing a well-formed ripgrep --json event.
        mock_executor = _mock_output_capped_executor("", captured_path)

        with patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ):
            await service._search_ripgrep(
                pattern="func",
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                context_lines=0,
                max_results=_TEST_MAX_RESULTS,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )

        assert "path" in captured_path, "executor was never invoked"
        assert not os.path.exists(captured_path["path"]), (
            "temp file leaked on the early-termination (output_capped) path"
        )
        assert service._last_search_read_capped is True

    @pytest.mark.asyncio
    async def test_grep_temp_file_removed_when_output_capped(self, tmp_path):
        """_search_grep must remove its temp file even when the executor
        reports output_capped=True (the subprocess was killed mid-write)."""
        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.side_effect = (
                lambda cmd: "/usr/bin/grep" if cmd == "grep" else None
            )
            service = RegexSearchService(tmp_path)

        captured_path: dict = {}
        mock_executor = _mock_output_capped_executor(
            "file.py:1:def func():\n", captured_path
        )

        with patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ):
            await service._search_grep(
                pattern="func",
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                context_lines=0,
                max_results=_TEST_MAX_RESULTS,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )

        assert "path" in captured_path, "executor was never invoked"
        assert not os.path.exists(captured_path["path"]), (
            "temp file leaked on the early-termination (output_capped) path"
        )
        assert service._last_search_read_capped is True
