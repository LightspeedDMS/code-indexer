"""
Unit tests for Bug #589: gather_repo_metrics runs git rev-list on non-git repos.

Bug: gather_repo_metrics() runs git ls-files and git rev-list unconditionally,
even on local:// repos that have no .git directory. This causes WARNING log spam
every refresh cycle.

Fix: Before running git commands, check if a .git directory exists. If not,
return (0, 0) immediately without running any git commands.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch


class TestGatherRepoMetricsNonGit:
    """Tests verifying Bug #589 fix: no git commands on non-git repos."""

    def test_non_git_path_returns_zero_zero(self):
        """
        gather_repo_metrics() must return (0, 0) for a path that has no .git
        directory, without running any subprocess commands.
        """
        from code_indexer.services.progress_subprocess_runner import gather_repo_metrics

        with tempfile.TemporaryDirectory() as tmpdir:
            # Confirm no .git directory exists
            assert not (Path(tmpdir) / ".git").exists()

            result = gather_repo_metrics(tmpdir)

        assert result == (0, 0), f"Expected (0, 0) for non-git path, got {result}"

    def test_non_git_path_does_not_call_subprocess(self):
        """
        gather_repo_metrics() must NOT call subprocess.run for non-git paths.
        Running git commands on non-git repos causes WARNING log spam.
        """
        from code_indexer.services.progress_subprocess_runner import gather_repo_metrics

        with tempfile.TemporaryDirectory() as tmpdir:
            assert not (Path(tmpdir) / ".git").exists()

            with patch("subprocess.run") as mock_run:
                result = gather_repo_metrics(tmpdir)

            (
                mock_run.assert_not_called(),
                ("subprocess.run was called for a non-git path — this is Bug #589"),
            )

        assert result == (0, 0)

    def test_git_repo_path_calls_subprocess_and_returns_counts(self):
        """
        gather_repo_metrics() must still call git commands and return counts
        for paths that DO have a .git directory.
        """
        from code_indexer.services.progress_subprocess_runner import gather_repo_metrics

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake .git directory to simulate a git repo
            (Path(tmpdir) / ".git").mkdir()

            mock_ls_result = subprocess.CompletedProcess(
                args=["git", "-C", tmpdir, "ls-files"],
                returncode=0,
                stdout="file1.py\nfile2.py\nfile3.py\n",
                stderr="",
            )
            mock_rev_result = subprocess.CompletedProcess(
                args=["git", "-C", tmpdir, "rev-list", "--count", "HEAD"],
                returncode=0,
                stdout="42\n",
                stderr="",
            )

            with patch(
                "subprocess.run", side_effect=[mock_ls_result, mock_rev_result]
            ) as mock_run:
                file_count, commit_count = gather_repo_metrics(tmpdir)

            assert mock_run.call_count == 2, (
                f"Expected 2 subprocess calls for git repo, got {mock_run.call_count}"
            )

        assert file_count == 3, f"Expected 3 files, got {file_count}"
        assert commit_count == 42, f"Expected 42 commits, got {commit_count}"

    def test_non_git_path_returns_integers(self):
        """
        gather_repo_metrics() must return a tuple of two integers even for
        non-git paths (type contract is maintained).
        """
        from code_indexer.services.progress_subprocess_runner import gather_repo_metrics

        with tempfile.TemporaryDirectory() as tmpdir:
            result = gather_repo_metrics(tmpdir)

        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        assert len(result) == 2, f"Expected 2-tuple, got length {len(result)}"
        assert isinstance(result[0], int), (
            f"Expected int file_count, got {type(result[0])}"
        )
        assert isinstance(result[1], int), (
            f"Expected int commit_count, got {type(result[1])}"
        )
