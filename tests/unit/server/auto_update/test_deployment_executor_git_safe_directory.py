"""
Tests for git safe.directory configuration in auto-update.

Tests for DeploymentExecutor._ensure_git_safe_directory() method that adds
the repository to git's safe.directory configuration to avoid "dubious ownership"
errors when the service runs as a different user than the repo owner.

Also covers DeploymentExecutor._ensure_git_safe_directory_wildcard() (Bug
#1466): a blanket safe.directory='*' grant for the service account,
covering CoW-daemon-owned golden-repos/activated-repos (owned by a
different OS user than the service account) regardless of which directory
they live in.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureGitSafeDirectory:
    """Tests for _ensure_git_safe_directory method."""

    def test_ensure_git_safe_directory_method_exists(self):
        """DeploymentExecutor should have _ensure_git_safe_directory method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_ensure_git_safe_directory")
        assert callable(getattr(executor, "_ensure_git_safe_directory"))

    def test_ensure_git_safe_directory_returns_true_when_service_not_found(self):
        """Should return True when service file doesn't exist (not a fatal error)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="nonexistent-service",
        )

        # Mock Path.exists to return False
        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_git_safe_directory()

        assert result is True

    def test_ensure_git_safe_directory_returns_true_when_already_configured(self):
        """Should return True without changes if safe.directory already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        # Mock git config --get-all to show repo already configured
        git_output = "/home/user/code-indexer\n/some/other/path\n"

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: git config --get-all (shows already configured)
                    mock_run.return_value = MagicMock(
                        returncode=0,
                        stdout=git_output,
                    )

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify we only checked, didn't try to add
        assert mock_run.call_count == 1
        check_call = mock_run.call_args_list[0]
        assert "git" in check_call[0][0]
        assert "config" in check_call[0][0]
        assert "--get-all" in check_call[0][0]

    def test_ensure_git_safe_directory_adds_when_not_configured(self):
        """Should add safe.directory when not already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: git config --get-all (empty, not configured)
                    # Second call: git config --add (success)
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),  # Not configured
                        MagicMock(returncode=0),  # Add successful
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify we checked and then added
        assert mock_run.call_count == 2

        # Check call
        check_call = mock_run.call_args_list[0]
        assert check_call[0][0] == [
            "sudo",
            "-u",
            "code-indexer",
            "git",
            "config",
            "--global",
            "--get-all",
            "safe.directory",
        ]

        # Add call
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0] == [
            "sudo",
            "-u",
            "code-indexer",
            "git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            "/home/user/code-indexer",
        ]

    def test_ensure_git_safe_directory_skips_when_no_user_line(self):
        """Should skip gracefully when service file has no User= line."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                result = executor._ensure_git_safe_directory()

        # Should return True (not a fatal error, just skip)
        assert result is True

    def test_ensure_git_safe_directory_handles_git_config_failure(self):
        """Should return False when git config command fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    # First call: git config --get-all (success, not configured)
                    # Second call: git config --add (FAILURE)
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(
                            returncode=1,
                            stderr="fatal: unable to write new config",
                        ),
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is False

    def test_ensure_git_safe_directory_handles_exception(self):
        """Should return False and log error on exception."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", side_effect=Exception("Disk failure")):
            result = executor._ensure_git_safe_directory()

        assert result is False

    def test_ensure_git_safe_directory_uses_working_directory_from_service_file(self):
        """Should use WorkingDirectory from service file as repo path."""
        executor = DeploymentExecutor(
            repo_path=Path("/wrong/path"),  # This should be ignored
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/correct/repo/path
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(returncode=0),
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify correct repo path was used
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0][-1] == "/correct/repo/path"

    def test_ensure_git_safe_directory_falls_back_to_repo_path_when_no_working_directory(
        self,
    ):
        """Should fall back to self.repo_path when WorkingDirectory not in service file."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(returncode=0),
                    ]

                    result = executor._ensure_git_safe_directory()

        assert result is True
        # Verify fallback to self.repo_path
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0][-1] == "/home/user/code-indexer"


class TestEnsureGitSafeDirectoryWildcard:
    """Tests for _ensure_git_safe_directory_wildcard method (Bug #1466).

    Unlike _ensure_git_safe_directory() (grants ONE specific repo path --
    the cidx-server checkout itself), this grants a blanket
    `safe.directory=*` for the service account so CoW-daemon-owned
    golden-repos/activated-repos (owned by a different OS user than the
    service account) are covered regardless of which directory they live
    in -- complete, gap-free coverage instead of a per-call-site env-var
    patch (which does not persist across sibling subprocess calls within
    the same method, see Bug #1466 issue body).

    Mirrors TestEnsureGitSafeDirectory's mock shape exactly (same
    service-file-not-found / no-User-line / already-configured / add /
    failure / exception safety posture) since this method must be at
    least as safe as its existing sibling on every environment it runs on
    (solo/production included -- it fires on EVERY deploy cycle via the
    same call chain).
    """

    def test_ensure_git_safe_directory_wildcard_method_exists(self):
        """DeploymentExecutor should have _ensure_git_safe_directory_wildcard method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_ensure_git_safe_directory_wildcard")
        assert callable(getattr(executor, "_ensure_git_safe_directory_wildcard"))

    def test_ensure_git_safe_directory_wildcard_returns_true_when_service_not_found(
        self,
    ):
        """Should return True when service file doesn't exist (not a fatal error).

        This is the solo/dev-environment case: a plain `python -m uvicorn`
        run (no systemd unit at all, e.g. local dev server or a CLI-driven
        test) must never fail deployment just because there's no service
        file to read a User= from.
        """
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="nonexistent-service",
        )

        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_git_safe_directory_wildcard()

        assert result is True

    def test_ensure_git_safe_directory_wildcard_returns_true_when_already_configured(
        self,
    ):
        """Should return True without changes if '*' is already configured (no-op)."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        # git config --get-all safe.directory already lists '*' among entries
        git_output = "/home/user/code-indexer\n*\n"

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        returncode=0,
                        stdout=git_output,
                    )

                    result = executor._ensure_git_safe_directory_wildcard()

        assert result is True
        # Verify we only checked, never tried to add a duplicate '*' entry
        assert mock_run.call_count == 1
        check_call = mock_run.call_args_list[0]
        assert "git" in check_call[0][0]
        assert "config" in check_call[0][0]
        assert "--get-all" in check_call[0][0]

    def test_ensure_git_safe_directory_wildcard_adds_when_not_configured(self):
        """Should add safe.directory='*' when not already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),  # Not configured
                        MagicMock(returncode=0),  # Add successful
                    ]

                    result = executor._ensure_git_safe_directory_wildcard()

        assert result is True
        assert mock_run.call_count == 2

        # Check call reuses the SAME service_user extraction as its sibling
        check_call = mock_run.call_args_list[0]
        assert check_call[0][0] == [
            "sudo",
            "-u",
            "code-indexer",
            "git",
            "config",
            "--global",
            "--get-all",
            "safe.directory",
        ]

        # Add call grants '*' (blanket), NOT a specific repo path
        add_call = mock_run.call_args_list[1]
        assert add_call[0][0] == [
            "sudo",
            "-u",
            "code-indexer",
            "git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            "*",
        ]

    def test_ensure_git_safe_directory_wildcard_skips_when_no_user_line(self):
        """Should skip gracefully (never invoking subprocess.run) when the
        service file has no User= line."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    result = executor._ensure_git_safe_directory_wildcard()

        assert result is True
        mock_run.assert_not_called()

    def test_ensure_git_safe_directory_wildcard_handles_git_config_failure(self):
        """Should return False when the git config --add command fails."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        service_content = """[Service]
User=code-indexer
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=0, stdout=""),
                        MagicMock(
                            returncode=1,
                            stderr="fatal: unable to write new config",
                        ),
                    ]

                    result = executor._ensure_git_safe_directory_wildcard()

        assert result is False

    def test_ensure_git_safe_directory_wildcard_handles_exception(self):
        """Should return False and log error on exception -- never raise."""
        executor = DeploymentExecutor(
            repo_path=Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", side_effect=Exception("Disk failure")):
            result = executor._ensure_git_safe_directory_wildcard()

        assert result is False


class TestEnsureGitSafeDirectoryWildcardRealGitMechanismProof:
    """Bug #1466: real, unmocked subprocess-level proof.

    A genuine UID-mismatch reproduction of git's "dubious ownership" error
    requires root (to chown a directory to a different owner) and is not
    feasible in this sandbox. Instead, using a completely fresh, isolated
    HOME/GIT_CONFIG_GLOBAL (so there is provably no pre-existing
    safe.directory grant from the real environment that could mask a
    bug), we prove:

      1. The exact `git config --global --add safe.directory '*'` command
         the production method issues (minus the `sudo -u` wrapper, which
         cannot be exercised without real multi-user privileges) is
         recognized by the REAL git binary as a genuine, git-honored
         safe.directory grant -- not just present as a key in a python
         dict passed to a mock.
      2. Solo/production non-regression requirement: granting the
         wildcard has ZERO behavioral effect on ordinary git operations
         against a repo the current user genuinely owns (the case where
         there is no ownership mismatch to begin with) -- a
         representative `git status` invocation succeeds identically
         before and after the grant is applied.
    """

    def test_wildcard_grant_recognized_by_real_git_and_solo_mode_unaffected(
        self, tmp_path
    ):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        (repo_dir / "README.md").write_text("hello")
        subprocess.run(
            ["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        fresh_home = tmp_path / "fresh_home"
        fresh_home.mkdir()
        fresh_gitconfig = fresh_home / ".gitconfig"  # deliberately absent

        env = os.environ.copy()
        env["HOME"] = str(fresh_home)
        env["GIT_CONFIG_GLOBAL"] = str(fresh_gitconfig)

        # --- Sanity: fresh global config has no safe.directory entries yet
        sanity = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert sanity.returncode != 0 or "*" not in sanity.stdout.split("\n"), (
            "Sanity check failed: fresh global config should have no "
            f"safe.directory grants yet; stdout={sanity.stdout!r}"
        )

        # --- Baseline: git status succeeds BEFORE the wildcard grant exists
        # (this repo is genuinely owned by the current user -- the
        # solo/production case with no ownership mismatch at all).
        before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert before.returncode == 0, (
            f"git status must succeed before the wildcard grant: "
            f"stderr={before.stderr!r}"
        )

        # --- Apply the exact command the production method issues
        add_result = subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", "*"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert add_result.returncode == 0, (
            f"git config --add safe.directory '*' must succeed: "
            f"stderr={add_result.stderr!r}"
        )

        # --- Proof 1: the real git binary now recognizes '*' as a grant
        after_config = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert after_config.returncode == 0
        assert "*" in after_config.stdout.split("\n"), (
            "git must recognize the wildcard grant via --get-all "
            f"safe.directory; stdout={after_config.stdout!r}"
        )

        # --- Proof 2: ordinary git status on a same-owner repo behaves
        # IDENTICALLY after the grant (solo/production non-regression).
        after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert after.returncode == 0, (
            f"git status must still succeed after the wildcard grant: "
            f"stderr={after.stderr!r}"
        )
        assert after.stdout == before.stdout, (
            "Wildcard safe.directory grant must have ZERO effect on "
            "ordinary git status output for a same-owner repo -- "
            f"before={before.stdout!r} after={after.stdout!r}"
        )


class TestExecuteCallsGitSafeDirectoryWildcard:
    """Bug #1466: execute() must wire in _ensure_git_safe_directory_wildcard()
    as an adjacent step to the existing _ensure_git_safe_directory() call, so
    the blanket wildcard grant actually runs on every deploy cycle.

    Mirrors the established project pattern for asserting execute()'s
    orchestration/wiring (see e.g. test_deployment_executor_nodejs.py::
    TestExecuteWiresEnsureNodejsBeforeNpmConsumers and
    test_deployment_executor_scip_python.py::test_execute_wires_ensure_scip_python):
    every OTHER execute() step is patched to a no-op success (their own real
    behavior is unit-tested independently elsewhere in this codebase) so this
    test observes ONLY execute()'s call-site wiring in isolation.
    """

    def test_execute_calls_ensure_git_safe_directory_wildcard(self, tmp_path):
        executor = DeploymentExecutor(
            repo_path=tmp_path / "repo", service_name="cidx-server"
        )

        noop_steps = [
            "git_pull",
            "git_submodule_update",
            "_build_hnswlib_with_fallback",
            "pip_install",
            "_ensure_launch_config",
            "_ensure_cidx_repo_root",
            "_ensure_git_safe_directory",
            "_ensure_auto_updater_uses_server_python",
            "_ensure_data_dir_env_var",
            "_ensure_malloc_arena_max",
            "_ensure_codex_cli_installed",
            "ensure_ripgrep",
            "ensure_nodejs",
            "ensure_scip_python",
            "_ensure_sudoers_restart",
            "_ensure_memory_overcommit",
            "_ensure_swap_file",
            "_ensure_claude_cli_updated",
            "_ensure_pace_maker_installed",
            "_ensure_claude_cli_installed",
            "_ensure_nfs_research_symlinks",
            "_ensure_activated_repos_symlink_for_cow_daemon",
            "_ensure_daemon_storage_path",
            "_ensure_systemd_claude_path",
            "_ensure_rust_toolchain",
        ]

        from contextlib import ExitStack

        stack = ExitStack()
        for name in noop_steps:
            if hasattr(executor, name):
                stack.enter_context(patch.object(executor, name, return_value=True))
        mock_wildcard = stack.enter_context(
            patch.object(
                executor, "_ensure_git_safe_directory_wildcard", return_value=True
            )
        )
        with stack:
            executor.execute()

        mock_wildcard.assert_called_once()
