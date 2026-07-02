"""Story #997 - Unit tests for DeploymentExecutor._ensure_pace_maker_installed().

Tests the non-fatal pace-maker install/update logic:
- Fresh install: git clone + install.sh + pace-maker off
- Update: git pull + install.sh, no pace-maker off
- Failure modes return False and deployment continues
- Bootstrap config gets pace_maker_clone_path recorded
- Server user triggers sudo -u pattern
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch


from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


def _make_executor(service_name: str = "cidx-server") -> DeploymentExecutor:
    """Build a minimal DeploymentExecutor instance without real side effects."""
    executor = DeploymentExecutor.__new__(DeploymentExecutor)
    executor.service_name = service_name
    return executor


def _make_completed_process(returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stderr = stderr
    proc.stdout = ""
    return proc


def _std_patches(tmp_path: Path):
    """Return standard patch triple for tests: Path.home, _extract_service_user=None, _cidx_data_dir."""
    return (
        patch(
            "code_indexer.server.auto_update.deployment_executor.Path.home",
            return_value=tmp_path,
        ),
        patch.object(DeploymentExecutor, "_extract_service_user", return_value=None),
        patch(
            "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
            tmp_path,
        ),
    )


class TestEnsurePaceMakerFreshInstall:
    """Tests for fresh install path (no .git directory present)."""

    def test_fresh_install_calls_git_clone(self, tmp_path: Path) -> None:
        """Fresh install must call git clone with PACE_MAKER_REPO_URL."""
        from code_indexer.server.auto_update.deployment_executor import (
            PACE_MAKER_REPO_URL,
        )

        executor = _make_executor()
        # Do NOT create clone_path -- fresh install
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone
                _make_completed_process(0),  # install.sh
                _make_completed_process(0),  # pace-maker off
            ]
            result = executor._ensure_pace_maker_installed()

        assert result is True
        clone_call = mock_run.call_args_list[0]
        assert clone_call[0][0][0] == "git"
        assert clone_call[0][0][1] == "clone"
        assert PACE_MAKER_REPO_URL in clone_call[0][0]

    def test_fresh_install_runs_install_sh(self, tmp_path: Path) -> None:
        """Fresh install must run install.sh after clone."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone
                _make_completed_process(0),  # install.sh
                _make_completed_process(0),  # pace-maker off
            ]
            executor._ensure_pace_maker_installed()

        install_call = mock_run.call_args_list[1]
        cmd = install_call[0][0]
        assert "install.sh" in " ".join(cmd)
        assert "bash" in cmd

    def test_fresh_install_runs_pace_maker_off(self, tmp_path: Path) -> None:
        """Fresh install must run 'pace-maker off' after install.sh."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone
                _make_completed_process(0),  # install.sh
                _make_completed_process(0),  # pace-maker off
            ]
            executor._ensure_pace_maker_installed()

        off_call = mock_run.call_args_list[2]
        assert off_call[0][0] == ["pace-maker", "off"]


class TestEnsurePaceMakerUpdate:
    """Tests for update path (.git directory already present)."""

    def test_update_calls_git_pull_not_clone(self, tmp_path: Path) -> None:
        """Update must call 'git -C <clone_path> pull', not 'git clone'."""
        executor = _make_executor()
        clone_path = tmp_path / "claude-pace-maker"
        clone_path.mkdir()
        (clone_path / ".git").mkdir()

        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git pull
                _make_completed_process(0),  # install.sh
            ]
            result = executor._ensure_pace_maker_installed()

        assert result is True
        pull_call = mock_run.call_args_list[0]
        pull_cmd = pull_call[0][0]
        # Must be: git -C <clone_path> pull
        assert pull_cmd[0] == "git"
        assert pull_cmd[1] == "-C"
        assert pull_cmd[2] == str(clone_path)
        assert pull_cmd[3] == "pull"
        # Verify no 'clone' call happened
        for c in mock_run.call_args_list:
            assert "clone" not in c[0][0]

    def test_update_does_not_run_pace_maker_off(self, tmp_path: Path) -> None:
        """Update must NOT run 'pace-maker off' (only fresh install does that)."""
        executor = _make_executor()
        clone_path = tmp_path / "claude-pace-maker"
        clone_path.mkdir()
        (clone_path / ".git").mkdir()

        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git pull
                _make_completed_process(0),  # install.sh
            ]
            executor._ensure_pace_maker_installed()

        all_cmds = [c[0][0] for c in mock_run.call_args_list]
        assert ["pace-maker", "off"] not in all_cmds


class TestEnsurePaceMakerFailures:
    """Tests for failure modes - all must return False."""

    def test_clone_failure_returns_false(self, tmp_path: Path) -> None:
        """git clone failure (non-zero exit) must return False."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.return_value = _make_completed_process(1, "clone failed")
            result = executor._ensure_pace_maker_installed()

        assert result is False

    def test_install_sh_failure_returns_false(self, tmp_path: Path) -> None:
        """install.sh failure (non-zero exit) must return False."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone succeeds
                _make_completed_process(1, "install failed"),  # install.sh fails
            ]
            result = executor._ensure_pace_maker_installed()

        assert result is False

    def test_timeout_returns_false(self, tmp_path: Path) -> None:
        """subprocess.TimeoutExpired during any operation must return False."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd="git clone", timeout=60
            )
            result = executor._ensure_pace_maker_installed()

        assert result is False

    def test_generic_exception_returns_false(self, tmp_path: Path) -> None:
        """Any unexpected exception must return False (non-fatal)."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with (
            p1,
            p2,
            p3,
            patch("subprocess.run", side_effect=OSError("permission denied")),
        ):
            result = executor._ensure_pace_maker_installed()

        assert result is False


class TestEnsurePaceMakerSudoEnv:
    """Tests that sudo path injects NONINTERACTIVE env var into the command."""

    def test_install_sh_via_sudo_includes_env_noninteractive(
        self, tmp_path: Path
    ) -> None:
        """When server_user is set, install.sh command must include 'env NONINTERACTIVE=1'."""
        executor = _make_executor()
        p1, p3 = (
            patch(
                "code_indexer.server.auto_update.deployment_executor.Path.home",
                return_value=tmp_path,
            ),
            patch(
                "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
                tmp_path,
            ),
        )
        with (
            p1,
            p3,
            patch.object(
                DeploymentExecutor, "_extract_service_user", return_value="code-indexer"
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone
                _make_completed_process(0),  # install.sh via sudo
                _make_completed_process(0),  # pace-maker off
            ]
            executor._ensure_pace_maker_installed()

        install_call = mock_run.call_args_list[1]
        cmd = install_call[0][0]
        # Must contain: sudo -u code-indexer env NONINTERACTIVE=1 bash install.sh
        assert "sudo" in cmd
        assert "-u" in cmd
        assert "code-indexer" in cmd
        assert "env" in cmd
        assert "NONINTERACTIVE=1" in cmd

    def test_install_sh_without_sudo_does_not_inject_env_cmd(
        self, tmp_path: Path
    ) -> None:
        """When no server_user, install.sh command must NOT contain 'env' or 'sudo'."""
        executor = _make_executor()
        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone
                _make_completed_process(0),  # install.sh
                _make_completed_process(0),  # pace-maker off
            ]
            executor._ensure_pace_maker_installed()

        install_call = mock_run.call_args_list[1]
        cmd = install_call[0][0]
        assert "sudo" not in cmd
        assert "env" not in cmd


class TestEnsurePaceMakerConfigWritten:
    """Tests that bootstrap config gets pace_maker_clone_path recorded."""

    def test_clone_path_written_to_config_on_fresh_install(
        self, tmp_path: Path
    ) -> None:
        """After successful install, config.json must contain pace_maker_clone_path."""
        executor = _make_executor()
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"server_dir": str(tmp_path)}))

        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git clone
                _make_completed_process(0),  # install.sh
                _make_completed_process(0),  # pace-maker off
            ]
            executor._ensure_pace_maker_installed()

        written = json.loads(config_path.read_text())
        assert "pace_maker_clone_path" in written
        expected_path = str(tmp_path / "claude-pace-maker")
        assert written["pace_maker_clone_path"] == expected_path

    def test_clone_path_written_to_config_on_update(self, tmp_path: Path) -> None:
        """After successful update, config.json must also contain pace_maker_clone_path."""
        executor = _make_executor()
        clone_path = tmp_path / "claude-pace-maker"
        clone_path.mkdir()
        (clone_path / ".git").mkdir()
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({}))

        p1, p2, p3 = _std_patches(tmp_path)
        with p1, p2, p3, patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_completed_process(0),  # git pull
                _make_completed_process(0),  # install.sh
            ]
            executor._ensure_pace_maker_installed()

        written = json.loads(config_path.read_text())
        assert "pace_maker_clone_path" in written
