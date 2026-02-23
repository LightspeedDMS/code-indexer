"""
Unit tests for GitPullUpdater divergent branch detection and auto-recovery (Story #272).

Tests:
1. Auto-recovery from divergent branch during git pull
2. Non-divergence git pull errors are NOT intercepted
3. force_reset=True skips git pull, runs fetch + reset --hard
4. Branch detection fallback to "main" when rev-parse fails
5. Recovery handles fetch/reset failures
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
# Helper: build a completed-process mock
# ---------------------------------------------------------------------------


def _proc(returncode=0, stdout="", stderr=""):
    result = Mock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# AC1: Auto-recovery from divergent branch
# ---------------------------------------------------------------------------


class TestDivergentBranchAutoRecovery:
    """
    AC1: When git pull fails with divergent branch error, GitPullUpdater
    must auto-recover by running fetch + reset --hard origin/{branch}.
    """

    def test_divergent_branch_triggers_auto_recovery(self, updater):
        """
        When git pull fails with 'divergent branches' in stderr,
        auto-recovery should run fetch + reset --hard.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(
            returncode=1,
            stderr="hint: You have divergent branches and need to specify how to reconcile them.",
        )
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,   # git status --porcelain
                git_pull_fail,   # git pull -> fails with divergent branches
                git_rev_parse,   # git rev-parse --abbrev-ref HEAD
                git_fetch,       # git fetch origin
                git_reset,       # git reset --hard origin/main
            ]
            # Should NOT raise - auto-recovery succeeds
            updater.update()

        calls = mock_run.call_args_list
        assert len(calls) == 5

        # Verify fetch was called
        fetch_call = calls[3]
        assert fetch_call[0][0] == ["git", "fetch", "origin"]

        # Verify reset --hard was called with correct branch
        reset_call = calls[4]
        assert reset_call[0][0] == ["git", "reset", "--hard", "origin/main"]

    def test_need_to_specify_reconcile_triggers_auto_recovery(self, updater):
        """
        When git pull fails with 'Need to specify how to reconcile' in stderr,
        auto-recovery should also trigger.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(
            returncode=1,
            stderr="Need to specify how to reconcile divergent branches.",
        )
        git_rev_parse = _proc(returncode=0, stdout="develop\n")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_pull_fail,
                git_rev_parse,
                git_fetch,
                git_reset,
            ]
            updater.update()  # Should not raise

        reset_call = mock_run.call_args_list[4]
        assert reset_call[0][0] == ["git", "reset", "--hard", "origin/develop"]

    def test_recovery_uses_detected_branch_name(self, updater):
        """
        Auto-recovery must use the actual branch name from git rev-parse,
        not hardcode 'main'.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(returncode=1, stderr="divergent branches")
        git_rev_parse = _proc(returncode=0, stdout="feature/my-feature\n")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_pull_fail,
                git_rev_parse,
                git_fetch,
                git_reset,
            ]
            updater.update()

        reset_call = mock_run.call_args_list[4]
        assert reset_call[0][0] == ["git", "reset", "--hard", "origin/feature/my-feature"]

    def test_successful_git_pull_no_auto_recovery(self, updater):
        """
        When git pull succeeds, no auto-recovery should happen (no fetch/reset calls).
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_ok]
            updater.update()

        # Only 2 calls: status + pull
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# AC2: Non-divergence git pull errors are not intercepted
# ---------------------------------------------------------------------------


class TestNonDivergentErrors:
    """
    AC2: Non-divergence git pull errors must raise RuntimeError
    with the original error message, without triggering auto-recovery.
    """

    def test_non_divergent_error_raises_runtime_error(self, updater):
        """
        A git pull failure with unrelated error must raise RuntimeError immediately.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(
            returncode=1,
            stderr="fatal: repository 'https://github.com/org/repo.git' not found",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_fail]

            with pytest.raises(RuntimeError) as exc_info:
                updater.update()

        assert "Git pull failed" in str(exc_info.value)
        # Only 2 calls: status + pull (no recovery attempt)
        assert mock_run.call_count == 2

    def test_authentication_error_not_intercepted(self, updater):
        """
        SSH authentication failure must propagate as RuntimeError, not trigger recovery.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(
            returncode=128,
            stderr="ERROR: Permission to org/repo.git denied to deploy-key.",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_fail]

            with pytest.raises(RuntimeError) as exc_info:
                updater.update()

        assert "Git pull failed" in str(exc_info.value)
        assert mock_run.call_count == 2

    def test_network_error_not_intercepted(self, updater):
        """
        Network failure must raise RuntimeError, not trigger auto-recovery.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(
            returncode=1,
            stderr="fatal: Could not read from remote repository.",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_fail]

            with pytest.raises(RuntimeError):
                updater.update()

        assert mock_run.call_count == 2

    def test_original_error_message_preserved(self, updater):
        """
        RuntimeError message must contain the original stderr from git pull.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        original_error = "some unique error that is not divergent"
        git_pull_fail = _proc(returncode=1, stderr=original_error)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_fail]

            with pytest.raises(RuntimeError) as exc_info:
                updater.update()

        assert original_error in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC3/AC4: force_reset=True path
# ---------------------------------------------------------------------------


class TestForceResetPath:
    """
    AC3/AC4: When force_reset=True, skip git pull entirely and run
    git fetch + git reset --hard origin/{branch}.
    """

    def test_force_reset_skips_git_pull(self, updater):
        """
        force_reset=True must NOT call git pull at all.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,   # git status --porcelain
                git_rev_parse,   # git rev-parse --abbrev-ref HEAD
                git_fetch,       # git fetch origin
                git_reset,       # git reset --hard origin/main
            ]
            updater.update(force_reset=True)

        calls = mock_run.call_args_list
        # Verify no "git pull" in any call
        for c in calls:
            cmd = c[0][0]
            assert cmd[:2] != ["git", "pull"], f"git pull was unexpectedly called: {cmd}"

    def test_force_reset_runs_fetch_then_reset(self, updater):
        """
        force_reset=True must run git fetch origin then git reset --hard origin/{branch}.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_rev_parse = _proc(returncode=0, stdout="master\n")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_rev_parse,
                git_fetch,
                git_reset,
            ]
            updater.update(force_reset=True)

        calls = mock_run.call_args_list
        fetch_cmd = calls[2][0][0]
        reset_cmd = calls[3][0][0]

        assert fetch_cmd == ["git", "fetch", "origin"]
        assert reset_cmd == ["git", "reset", "--hard", "origin/master"]

    def test_force_reset_uses_branch_from_rev_parse(self, updater):
        """
        force_reset=True must detect branch name via git rev-parse --abbrev-ref HEAD.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_rev_parse = _proc(returncode=0, stdout="release/v2.0\n")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_rev_parse,
                git_fetch,
                git_reset,
            ]
            updater.update(force_reset=True)

        reset_cmd = mock_run.call_args_list[3][0][0]
        assert reset_cmd == ["git", "reset", "--hard", "origin/release/v2.0"]

    def test_force_reset_false_by_default(self, updater):
        """
        update() without arguments must use normal pull path (force_reset=False).
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_ok = _proc(returncode=0, stdout="Already up to date.")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [git_status_ok, git_pull_ok]
            # Should use normal pull path (no rev-parse, no fetch, no reset)
            updater.update()

        assert mock_run.call_count == 2
        pull_cmd = mock_run.call_args_list[1][0][0]
        assert pull_cmd == ["git", "pull"]


# ---------------------------------------------------------------------------
# AC6: Branch detection fallback
# ---------------------------------------------------------------------------


class TestBranchDetectionFallback:
    """
    AC6: When git rev-parse --abbrev-ref HEAD fails, fall back to "main".
    """

    def test_force_reset_fallback_to_main_when_rev_parse_fails(self, updater):
        """
        When git rev-parse fails (non-zero return), force_reset should
        fall back to 'main' as branch name.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_rev_parse_fail = _proc(returncode=128, stderr="fatal: not a git repository")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_rev_parse_fail,
                git_fetch,
                git_reset,
            ]
            updater.update(force_reset=True)

        reset_cmd = mock_run.call_args_list[3][0][0]
        assert reset_cmd == ["git", "reset", "--hard", "origin/main"]

    def test_divergent_recovery_fallback_to_main_when_rev_parse_fails(self, updater):
        """
        When git pull fails with divergent branch and rev-parse also fails,
        auto-recovery should fall back to 'main'.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(returncode=1, stderr="divergent branches")
        git_rev_parse_fail = _proc(returncode=128, stderr="fatal: not a git repository")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_pull_fail,
                git_rev_parse_fail,
                git_fetch,
                git_reset,
            ]
            updater.update()

        reset_cmd = mock_run.call_args_list[4][0][0]
        assert reset_cmd == ["git", "reset", "--hard", "origin/main"]

    def test_force_reset_fallback_when_rev_parse_raises_timeout(self, updater):
        """
        When git rev-parse times out, fall back to 'main'.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_fetch = _proc(returncode=0)
        git_reset = _proc(returncode=0)

        def side_effect(cmd, **kwargs):
            if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            if cmd[:2] == ["git", "status"]:
                return git_status_ok
            if cmd[:2] == ["git", "fetch"]:
                return git_fetch
            if cmd[:2] == ["git", "reset"]:
                return git_reset
            return _proc()

        with patch("subprocess.run", side_effect=side_effect):
            updater.update(force_reset=True)


# ---------------------------------------------------------------------------
# Recovery failure handling
# ---------------------------------------------------------------------------


class TestRecoveryFailureHandling:
    """
    Tests for fetch/reset failures during auto-recovery.
    """

    def test_fetch_failure_during_auto_recovery_raises_error(self, updater):
        """
        If git fetch fails during divergent auto-recovery, raise RuntimeError.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(returncode=1, stderr="divergent branches")
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch_fail = _proc(returncode=1, stderr="fatal: could not read from remote")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_pull_fail,
                git_rev_parse,
                git_fetch_fail,
            ]

            with pytest.raises(RuntimeError) as exc_info:
                updater.update()

        error_msg = str(exc_info.value).lower()
        assert "fetch" in error_msg or "auto-recovery" in error_msg or "git" in error_msg

    def test_reset_failure_during_auto_recovery_raises_error(self, updater):
        """
        If git reset --hard fails during divergent auto-recovery, raise RuntimeError.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_pull_fail = _proc(returncode=1, stderr="divergent branches")
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch = _proc(returncode=0)
        git_reset_fail = _proc(returncode=1, stderr="error: could not reset")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_pull_fail,
                git_rev_parse,
                git_fetch,
                git_reset_fail,
            ]

            with pytest.raises(RuntimeError) as exc_info:
                updater.update()

        error_msg = str(exc_info.value).lower()
        assert "reset" in error_msg or "auto-recovery" in error_msg or "git" in error_msg

    def test_fetch_failure_during_force_reset_raises_error(self, updater):
        """
        If git fetch fails during force_reset, raise RuntimeError.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch_fail = _proc(returncode=1, stderr="fatal: network error")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_rev_parse,
                git_fetch_fail,
            ]

            with pytest.raises(RuntimeError):
                updater.update(force_reset=True)

    def test_reset_failure_during_force_reset_raises_error(self, updater):
        """
        If git reset --hard fails during force_reset, raise RuntimeError.
        """
        git_status_ok = _proc(returncode=0, stdout="")
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch = _proc(returncode=0)
        git_reset_fail = _proc(returncode=1, stderr="error: reset failed")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_ok,
                git_rev_parse,
                git_fetch,
                git_reset_fail,
            ]

            with pytest.raises(RuntimeError):
                updater.update(force_reset=True)


# ---------------------------------------------------------------------------
# AC1: Existing Story #726 defense-in-depth preserved
# ---------------------------------------------------------------------------


class TestStory726DefensePreserved:
    """
    AC1: The Story #726 defense-in-depth (local modifications reset before pull)
    must still work correctly in all paths.
    """

    def test_local_modifications_reset_before_pull(self, updater):
        """
        When git status --porcelain shows local modifications,
        git reset --hard HEAD must be called before git pull.
        """
        git_status_dirty = _proc(returncode=0, stdout="M some/file.py\n")
        git_reset_head = _proc(returncode=0)
        git_pull_ok = _proc(returncode=0, stdout="Updating 1234567..abcdefg")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_dirty,
                git_reset_head,
                git_pull_ok,
            ]
            updater.update()

        calls = mock_run.call_args_list
        assert len(calls) == 3
        reset_head_cmd = calls[1][0][0]
        assert reset_head_cmd == ["git", "reset", "--hard", "HEAD"]

    def test_local_modifications_reset_before_force_reset(self, updater):
        """
        When force_reset=True and there are local modifications,
        git reset --hard HEAD must also be called first (defense-in-depth preserved).
        """
        git_status_dirty = _proc(returncode=0, stdout="M some/file.py\n")
        git_reset_head = _proc(returncode=0)
        git_rev_parse = _proc(returncode=0, stdout="main\n")
        git_fetch = _proc(returncode=0)
        git_reset_origin = _proc(returncode=0)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                git_status_dirty,
                git_reset_head,
                git_rev_parse,
                git_fetch,
                git_reset_origin,
            ]
            updater.update(force_reset=True)

        calls = mock_run.call_args_list
        # Second call should be git reset --hard HEAD
        assert calls[1][0][0] == ["git", "reset", "--hard", "HEAD"]
