"""
Unit tests for GitPullUpdater untracked file filtering (Bug #380).

Verifies that ?? (untracked) lines from git status --porcelain are ignored
when deciding whether to warn and reset before pull.
"""

import subprocess
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.git_pull_updater import GitPullUpdater


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_path(tmp_path):
    """Create a temporary repository directory."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    return repo


@pytest.fixture
def updater(repo_path):
    """Create a GitPullUpdater for the test repo."""
    return GitPullUpdater(str(repo_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(returncode=0, stdout="", stderr=""):
    result = Mock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Bug #380: Untracked file filtering
# ---------------------------------------------------------------------------


class TestUntrackedFileFiltering:
    """
    Bug #380: Untracked files (?? prefix) must not trigger warning or reset.
    Only tracked modifications (M, A, D, etc.) should trigger those.
    """

    def test_untracked_only_no_reset_before_pull(self, updater):
        """
        When git status shows only untracked files (??), no git reset --hard HEAD
        should be called before git pull.
        """
        git_status = _proc(returncode=0, stdout="?? .code-indexer-override.yaml\n")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        calls = mock_run.call_args_list
        # Only 2 calls: status + pull (no reset --hard HEAD)
        assert len(calls) == 2
        cmds = [c[0][0] for c in calls]
        assert ["git", "reset", "--hard", "HEAD"] not in cmds

    def test_untracked_only_pull_proceeds_normally(self, updater):
        """
        When git status shows only untracked files, git pull must still be called.
        """
        git_status = _proc(returncode=0, stdout="?? some_untracked_file.txt\n")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        pull_cmd = mock_run.call_args_list[1][0][0]
        assert pull_cmd == ["git", "pull"]

    def test_multiple_untracked_files_no_reset(self, updater):
        """
        Multiple untracked files must all be ignored - no reset triggered.
        """
        git_status = _proc(
            returncode=0,
            stdout="?? .code-indexer-override.yaml\n?? temp_file.py\n?? notes.txt\n",
        )
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        assert mock_run.call_count == 2
        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert ["git", "reset", "--hard", "HEAD"] not in cmds

    def test_tracked_modification_still_triggers_reset(self, updater):
        """
        A tracked modification (M prefix) must still trigger git reset --hard HEAD.
        Regression guard: the fix must not suppress legitimate resets.
        """
        git_status = _proc(returncode=0, stdout=" M tracked_file.py\n")
        git_reset_head = _proc(returncode=0)
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_reset_head, git_pull_ok]
            updater.update()

        assert mock_run.call_count == 3
        reset_cmd = mock_run.call_args_list[1][0][0]
        assert reset_cmd == ["git", "reset", "--hard", "HEAD"]

    def test_mixed_tracked_and_untracked_triggers_reset(self, updater):
        """
        When git status has both tracked modifications and untracked files,
        the tracked modification must still trigger git reset --hard HEAD.
        """
        git_status = _proc(
            returncode=0,
            stdout=" M tracked_file.py\n?? .code-indexer-override.yaml\n",
        )
        git_reset_head = _proc(returncode=0)
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_reset_head, git_pull_ok]
            updater.update()

        assert mock_run.call_count == 3
        reset_cmd = mock_run.call_args_list[1][0][0]
        assert reset_cmd == ["git", "reset", "--hard", "HEAD"]
