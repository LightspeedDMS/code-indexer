"""
Tests for GitPullUpdater fetch error behavior.

Tests that has_changes() raises GitFetchError (with correct classification)
instead of silently returning False when git fetch fails.

Story #295: Auto-Recovery for Corrupted Golden Repo Git Object Database.
"""

import pytest
from unittest.mock import patch, MagicMock

from code_indexer.global_repos.git_pull_updater import GitPullUpdater
from code_indexer.global_repos.git_error_classifier import GitFetchError


class TestGitPullUpdaterFetchError:
    """Test that has_changes() raises GitFetchError on fetch failure."""

    def test_has_changes_raises_git_fetch_error_on_fetch_failure(self, tmp_path):
        """
        has_changes() must raise GitFetchError when git fetch returns non-zero.

        Story #295 AC2: Instead of silently returning False on fetch failure,
        raise GitFetchError so the caller can decide whether to re-clone.
        """
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

        updater = GitPullUpdater(str(repo_path))

        with patch("subprocess.run") as mock_run:
            # git fetch returns non-zero (fetch failure)
            mock_run.return_value = MagicMock(
                returncode=128,
                stdout="",
                stderr="fatal: some git error",
            )

            with pytest.raises(GitFetchError):
                updater.has_changes()

    def test_has_changes_classifies_corruption_correctly(self, tmp_path):
        """
        has_changes() raises GitFetchError with category='corruption' for pack errors.

        Story #295 AC2: Error classification must be accurate so _handle_fetch_error
        can immediately trigger re-clone for corruption vs. wait for threshold for
        transient errors.
        """
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

        updater = GitPullUpdater(str(repo_path))

        corruption_stderr = (
            "error: Could not read d670460b4b4aece5915caf5c68d12f560a9fe3e4\n"
            "fatal: pack has 3 unresolved deltas"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128,
                stdout="",
                stderr=corruption_stderr,
            )

            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

            assert exc_info.value.category == "corruption"
            assert exc_info.value.stderr == corruption_stderr

    def test_has_changes_classifies_transient_correctly(self, tmp_path):
        """
        has_changes() raises GitFetchError with category='transient' for network errors.

        Story #295 AC2: Transient errors (network, auth) require 3 consecutive
        failures before triggering re-clone.
        """
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()

        updater = GitPullUpdater(str(repo_path))

        transient_stderr = (
            "fatal: unable to access 'https://github.com/org/repo.git/': "
            "Could not resolve host: github.com"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128,
                stdout="",
                stderr=transient_stderr,
            )

            with pytest.raises(GitFetchError) as exc_info:
                updater.has_changes()

            assert exc_info.value.category == "transient"
            assert exc_info.value.stderr == transient_stderr
