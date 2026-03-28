"""
Tests for Bug #569: git_push pushed_commits count fix.

Verifies that _count_pushed_commits() parses result.stderr (not stdout)
for the ref-update pattern and uses git rev-list --count for accurate
commit counting.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.services.git_operations_service import GitOperationsService


class TestCountPushedCommits:
    """Tests for _count_pushed_commits() helper method."""

    def _make_service(self):
        return GitOperationsService.__new__(GitOperationsService)

    def _make_result(self, stdout="", stderr=""):
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.stdout = stdout
        result.stderr = stderr
        return result

    def test_parses_stderr_for_ref_update_pattern(self):
        """Bug #569: Must check stderr, not stdout, for oldref..newref."""
        svc = self._make_service()
        result = self._make_result(
            stdout="",
            stderr="To github.com:org/repo.git\n   abc1234..def5678  main -> main\n",
        )
        rev_list_result = MagicMock()
        rev_list_result.stdout = "3\n"

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=rev_list_result,
        ) as mock_run:
            count = svc._count_pushed_commits(result, Path("/repo"))
            assert count == 3
            # Verify rev-list was called with the refs from stderr
            call_args = mock_run.call_args[0][0]
            assert "rev-list" in call_args
            assert "--count" in call_args
            assert "abc1234..def5678" in call_args

    def test_returns_zero_when_no_ref_pattern(self):
        """No ref-update pattern means nothing was pushed."""
        svc = self._make_service()
        result = self._make_result(stdout="", stderr="Everything up-to-date\n")
        count = svc._count_pushed_commits(result, Path("/repo"))
        assert count == 0

    def test_returns_zero_when_stderr_is_none(self):
        """Handles None stderr gracefully."""
        svc = self._make_service()
        result = self._make_result(stdout="", stderr=None)
        count = svc._count_pushed_commits(result, Path("/repo"))
        assert count == 0

    def test_returns_zero_when_stderr_is_empty(self):
        """Handles empty stderr."""
        svc = self._make_service()
        result = self._make_result(stdout="", stderr="")
        count = svc._count_pushed_commits(result, Path("/repo"))
        assert count == 0

    def test_fallback_to_one_when_revlist_fails(self):
        """Falls back to 1 when rev-list fails (push succeeded)."""
        svc = self._make_service()
        result = self._make_result(
            stderr="   abc1234..def5678  main -> main\n",
        )
        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            side_effect=subprocess.CalledProcessError(1, "git rev-list"),
        ):
            count = svc._count_pushed_commits(result, Path("/repo"))
            assert count == 1

    def test_ignores_stdout_ref_pattern(self):
        """Bug #569 regression: ref pattern in stdout must be ignored."""
        svc = self._make_service()
        result = self._make_result(
            stdout="abc1234..def5678  main -> main\n",
            stderr="Everything up-to-date\n",
        )
        count = svc._count_pushed_commits(result, Path("/repo"))
        assert count == 0  # stdout pattern should NOT be counted

    def test_handles_force_push_pattern(self):
        """Force push uses + prefix: '+abc1234...def5678'."""
        svc = self._make_service()
        result = self._make_result(
            stderr=" + abc1234...def5678 main -> main (forced update)\n",
        )
        rev_list_result = MagicMock()
        rev_list_result.stdout = "5\n"

        with patch(
            "code_indexer.server.services.git_operations_service.run_git_command",
            return_value=rev_list_result,
        ):
            count = svc._count_pushed_commits(result, Path("/repo"))
            assert count == 5
