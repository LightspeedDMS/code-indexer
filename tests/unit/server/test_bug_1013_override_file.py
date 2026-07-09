"""
Unit tests for Bug #1013 — cidx init creates override file in golden repos,
blocking auto-refresh.

Tests cover three levels of the fix:
  Level 1: --no-override-file flag suppresses .code-indexer-override.yaml creation
  Level 2: Pre-pull cleanup removes known cidx artifacts before git pull
  Level 3: Error recovery when git pull fails with "untracked files" error
"""

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.global_repos.git_pull_updater import GitPullUpdater


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
# Level 1 — CLI flag: --no-override-file
# ---------------------------------------------------------------------------


class TestNoOverrideFileFlag:
    """
    Level 1: --no-override-file suppresses creation of .code-indexer-override.yaml.
    """

    def test_no_override_file_flag_suppresses_creation(self, tmp_path):
        """
        cidx init --no-override-file must NOT create .code-indexer-override.yaml.
        """
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(
                cli,
                ["init", "--no-override-file"],
                catch_exceptions=False,
            )
            override_file = Path(td) / ".code-indexer-override.yaml"
            assert result.exit_code == 0, result.output
            assert not override_file.exists(), (
                "--no-override-file must suppress .code-indexer-override.yaml creation"
            )

    def test_default_init_creates_override_file(self, tmp_path):
        """
        cidx init without --no-override-file still creates .code-indexer-override.yaml
        (backward compatibility).
        """
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(
                cli,
                ["init"],
                catch_exceptions=False,
            )
            override_file = Path(td) / ".code-indexer-override.yaml"
            assert result.exit_code == 0, result.output
            assert override_file.exists(), (
                "Default cidx init must still create .code-indexer-override.yaml"
            )

    def test_no_override_file_language_mappings_still_created(self, tmp_path):
        """
        --no-override-file only suppresses the override file.
        language-mappings.yaml is a separate artifact and is not affected.
        """
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(
                cli,
                ["init", "--no-override-file"],
                catch_exceptions=False,
            )
            # language-mappings.yaml lives inside .code-indexer/
            lang_mappings = Path(td) / ".code-indexer" / "language-mappings.yaml"
            assert result.exit_code == 0, result.output
            assert lang_mappings.exists(), (
                "language-mappings.yaml must still be created when --no-override-file is used"
            )


# ---------------------------------------------------------------------------
# Level 2 — Pre-pull cleanup of known cidx untracked artifacts
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


class TestPrePullCleanup:
    """
    Level 2: Before git pull, known cidx untracked artifacts are removed.
    """

    def test_override_file_removed_before_pull(self, updater, repo_path):
        """
        When .code-indexer-override.yaml is untracked in the repo, it is removed
        before git pull to avoid 'would be overwritten by merge' errors.
        """
        override_file = repo_path / ".code-indexer-override.yaml"
        override_file.write_text("# cidx artifact")

        git_status = _proc(returncode=0, stdout="?? .code-indexer-override.yaml\n")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        assert not override_file.exists(), (
            ".code-indexer-override.yaml must be removed before git pull"
        )

    def test_language_mappings_removed_before_pull(self, updater, repo_path):
        """
        When language-mappings.yaml is untracked in the repo, it is removed
        before git pull.
        """
        lang_file = repo_path / "language-mappings.yaml"
        lang_file.write_text("# cidx artifact")

        git_status = _proc(returncode=0, stdout="?? language-mappings.yaml\n")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        assert not lang_file.exists(), (
            "language-mappings.yaml must be removed before git pull"
        )

    def test_non_cidx_untracked_files_not_removed(self, updater, repo_path):
        """
        Only known cidx artifacts are removed. Other untracked files are left alone.
        """
        other_file = repo_path / "my_notes.txt"
        other_file.write_text("user notes")

        git_status = _proc(returncode=0, stdout="?? my_notes.txt\n")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        assert other_file.exists(), (
            "Non-cidx untracked files must NOT be removed before git pull"
        )

    def test_cleanup_only_if_file_is_untracked(self, updater, repo_path):
        """
        Cleanup only removes files that appear as untracked (??) in git status.
        Files not in git status are left alone even if they are cidx artifacts.
        """
        override_file = repo_path / ".code-indexer-override.yaml"
        override_file.write_text("# cidx artifact")

        git_status = _proc(returncode=0, stdout="")  # clean
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_ok]
            updater.update()

        assert override_file.exists(), (
            "Cidx artifacts must only be removed if they appear as untracked in git status"
        )


# ---------------------------------------------------------------------------
# Level 3 — Error recovery from "untracked files would be overwritten"
# ---------------------------------------------------------------------------


class TestUntrackedFilesErrorRecovery:
    """
    Level 3: When git pull fails with 'untracked working tree files would be
    overwritten by merge', parse conflicting filenames, remove them, retry once.
    """

    UNTRACKED_ERROR = (
        "error: The following untracked working tree files would be overwritten "
        "by merge:\n\t.code-indexer-override.yaml\nPlease move or remove them "
        "before you merge.\nAborting"
    )

    def test_recovery_removes_conflicting_file_and_retries(self, updater, repo_path):
        """
        When git pull fails with untracked overwrite error, the conflicting file
        is removed and git pull is retried once.
        """
        override_file = repo_path / ".code-indexer-override.yaml"
        override_file.write_text("# cidx artifact")

        git_status = _proc(returncode=0, stdout="")  # no pre-pull cleanup needed
        git_pull_fail = _proc(returncode=1, stderr=self.UNTRACKED_ERROR)
        git_pull_retry_ok = _proc(returncode=0, stdout="Updating abc..def")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_fail, git_pull_retry_ok]
            updater.update()

        assert not override_file.exists(), (
            "Conflicting file must be removed after error recovery"
        )
        pull_calls = [c for c in mock_run.call_args_list if c[0][0] == ["git", "pull"]]
        assert len(pull_calls) == 2, (
            "git pull must be retried once after error recovery"
        )

    def test_recovery_parses_multiple_conflicting_files(self, updater, repo_path):
        """
        When multiple files are listed in the error, all are removed before retry.
        """
        override_file = repo_path / ".code-indexer-override.yaml"
        override_file.write_text("# cidx artifact")
        lang_file = repo_path / "language-mappings.yaml"
        lang_file.write_text("# cidx artifact")

        multi_error = (
            "error: The following untracked working tree files would be overwritten "
            "by merge:\n\t.code-indexer-override.yaml\n\tlanguage-mappings.yaml\n"
            "Please move or remove them before you merge.\nAborting"
        )

        git_status = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(returncode=1, stderr=multi_error)
        git_pull_retry_ok = _proc(returncode=0, stdout="Updating abc..def")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_fail, git_pull_retry_ok]
            updater.update()

        assert not override_file.exists(), ".code-indexer-override.yaml must be removed"
        assert not lang_file.exists(), "language-mappings.yaml must be removed"

    def test_non_overwrite_error_still_raises(self, updater, repo_path):
        """
        git pull failures unrelated to untracked overwrite errors must still raise
        RuntimeError (no silent swallowing).
        """
        git_status = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(
            returncode=1, stderr="fatal: repository 'origin' not found"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_fail]
            with pytest.raises(RuntimeError, match="Git pull failed"):
                updater.update()

    def test_retry_failure_raises(self, updater, repo_path):
        """
        If the retry after error recovery also fails, RuntimeError is raised.
        """
        override_file = repo_path / ".code-indexer-override.yaml"
        override_file.write_text("# cidx artifact")

        git_status = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(returncode=1, stderr=self.UNTRACKED_ERROR)
        git_pull_retry_fail = _proc(returncode=1, stderr="fatal: cannot merge diverged")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status, git_pull_fail, git_pull_retry_fail]
            with pytest.raises(RuntimeError):
                updater.update()

    def test_fetch_and_reset_path_also_cleans_untracked(self, updater, repo_path):
        """
        The _fetch_and_reset() path (used for divergent branch recovery and force reset)
        also cleans up cidx untracked artifacts before running git reset --hard.
        """
        override_file = repo_path / ".code-indexer-override.yaml"
        override_file.write_text("# cidx artifact")

        git_status = _proc(returncode=0, stdout="")  # no pre-pull cleanup
        git_pull_fail = _proc(
            returncode=1,
            stderr="divergent branches",
        )
        git_rev_parse = _proc(returncode=0, stdout="main")
        git_fetch = _proc(returncode=0)

        RESET_UNTRACKED_ERROR = (
            "error: The following untracked working tree files would be overwritten "
            "by checkout:\n\t.code-indexer-override.yaml\nPlease move or remove them "
            "before you switch branches.\nAborting"
        )
        git_reset_fail = _proc(returncode=1, stderr=RESET_UNTRACKED_ERROR)
        git_reset_ok = _proc(returncode=0, stdout="HEAD is now at abc123")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status,  # status check
                git_pull_fail,  # pull fails with divergent
                git_rev_parse,  # detect branch
                git_fetch,  # fetch origin
                git_reset_fail,  # reset --hard fails with untracked
                git_reset_ok,  # reset --hard succeeds after cleanup
            ]
            updater.update()

        assert not override_file.exists(), (
            "Cidx artifact must be removed during _fetch_and_reset error recovery"
        )
