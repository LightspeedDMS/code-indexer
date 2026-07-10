"""Tests for systemd/sudo control-plane timeout robustness in auto-update.

On a freshly-built staging host, the FIRST auto-update deploy compiles
hnswlib and installs rustup/ripgrep/Claude-CLI/pace-maker -- CPU-heavy work
that transiently starves systemd/sudo (pam_systemd blocks on a busy PID 1).
Several sudo/systemctl subprocess calls in DeploymentExecutor carried only a
30s (or 10s) timeout, so they raised subprocess.TimeoutExpired, were
swallowed by a broad except Exception, and silently skipped real config
steps (DEPLOY-GENERAL-034, DEPLOY-GENERAL-056).

This test module verifies:
1. The widened SYSTEMD_OP_TIMEOUT_SECONDS constant (>= 120).
2. The new _run_systemd_op_with_retry() helper retries ONLY on
   subprocess.TimeoutExpired, never on a completed process with a nonzero
   returncode.
3. The specific call sites that route through the helper
   (_write_service_file_and_reload, _ensure_sudoers_restart) retry-then-
   succeed and retry-then-exhaust-fail-soft correctly.
4. The call sites that are widened-only (no retry wrapping)
   (_restart_auto_update_service, _get_server_python, _read_service_file)
   use the new timeout value.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from code_indexer.server.auto_update.deployment_executor import (
    DeploymentExecutor,
    SYSTEMD_OP_TIMEOUT_SECONDS,
)


class TestSystemdOpTimeoutConstant:
    """Tests for the SYSTEMD_OP_TIMEOUT_SECONDS module constant."""

    def test_systemd_op_timeout_constant_is_at_least_120(self):
        """The widened control-plane timeout must be >= 120s to survive the
        CPU-starved first-deploy window on a freshly-built host."""
        assert SYSTEMD_OP_TIMEOUT_SECONDS >= 120


class TestRestartAutoUpdateServiceWidenedTimeout:
    """Scope item 4: widen-only, no retry wrapping, for _restart_auto_update_service."""

    def test_restart_auto_update_service_uses_widened_timeout(self):
        """_restart_auto_update_service must pass the new (widened) timeout
        value to subprocess.run, with a single attempt (no retry helper)."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = executor._restart_auto_update_service()

        assert result is True
        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["timeout"] == SYSTEMD_OP_TIMEOUT_SECONDS


class TestGetServerPythonAndReadServiceFileWidenedTimeout:
    """Scope item 4: widen-only (was timeout=10) for _get_server_python and
    _read_service_file -- no retry wrapping."""

    def test_get_server_python_uses_widened_timeout(self):
        """_get_server_python's sudo cat call must use the widened timeout
        (was 10s)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp/test-repo"), service_name="cidx-server"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

            executor._get_server_python()

        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["timeout"] == SYSTEMD_OP_TIMEOUT_SECONDS

    def test_read_service_file_uses_widened_timeout(self):
        """_read_service_file's sudo cat call must use the widened timeout
        (was 10s)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp/test-repo"), service_name="cidx-server"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="content", stderr="")

            executor._read_service_file(Path("/etc/systemd/system/cidx-server.service"))

        mock_run.assert_called_once()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["timeout"] == SYSTEMD_OP_TIMEOUT_SECONDS


class TestWriteServiceFileAndReloadRetry:
    """_write_service_file_and_reload routes sudo tee + daemon-reload through
    the retry helper (scope item 3)."""

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_write_service_file_and_reload_retries_tee_on_timeout_then_succeeds(
        self, mock_sleep
    ):
        """The sudo tee step retries once after subprocess.TimeoutExpired,
        then daemon-reload succeeds normally; overall result is True."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd=["sudo", "tee"], timeout=120),
                MagicMock(returncode=0),  # tee succeeds on retry
                MagicMock(returncode=0),  # daemon-reload succeeds
            ]

            result = executor._write_service_file_and_reload(
                Path("/etc/systemd/system/cidx-server.service"), "content"
            )

        assert result is True
        assert mock_run.call_count == 3
        mock_sleep.assert_called_once()

        # Both tee attempts must use the widened timeout.
        first_tee_kwargs = mock_run.call_args_list[0].kwargs
        retried_tee_kwargs = mock_run.call_args_list[1].kwargs
        assert first_tee_kwargs["timeout"] == SYSTEMD_OP_TIMEOUT_SECONDS
        assert retried_tee_kwargs["timeout"] == SYSTEMD_OP_TIMEOUT_SECONDS
        assert mock_run.call_args_list[1].args[0] == [
            "sudo",
            "tee",
            "/etc/systemd/system/cidx-server.service",
        ]

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_write_service_file_and_reload_retries_daemon_reload_on_timeout_then_succeeds(
        self, mock_sleep
    ):
        """Tee succeeds on the first attempt; daemon-reload retries once
        after subprocess.TimeoutExpired then succeeds; overall result True."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # tee succeeds
                subprocess.TimeoutExpired(
                    cmd=["sudo", "systemctl", "daemon-reload"], timeout=120
                ),
                MagicMock(returncode=0),  # daemon-reload succeeds on retry
            ]

            result = executor._write_service_file_and_reload(
                Path("/etc/systemd/system/cidx-server.service"), "content"
            )

        assert result is True
        assert mock_run.call_count == 3
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_write_service_file_and_reload_returns_false_when_tee_always_times_out(
        self, mock_sleep
    ):
        """When the tee step times out on EVERY attempt, the method must
        fail-soft: return False (not raise), after exhausting all attempts."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["sudo", "tee"], timeout=120
            )

            result = executor._write_service_file_and_reload(
                Path("/etc/systemd/system/cidx-server.service"), "content"
            )

        assert result is False
        assert mock_run.call_count == 3  # max_attempts, retries exhausted


class TestEnsureSudoersRestartRetry:
    """_ensure_sudoers_restart routes ALL its sudo subprocess calls through
    the retry helper (scope item 3): cat (verify), tee (create), chmod,
    visudo, and both cleanup rm -f calls."""

    SERVICE_CONTENT = """[Service]
User=jsbattig
WorkingDirectory=/home/user/code-indexer
ExecStart=/usr/bin/python3 -m uvicorn app:app
"""

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_sudoers_restart_retries_cat_on_timeout_then_succeeds(
        self, mock_sleep
    ):
        """The verify (sudo cat) step retries once after TimeoutExpired, then
        falls through to the create-rule path, completing successfully."""
        from pathlib import Path as _Path

        executor = DeploymentExecutor(
            repo_path=_Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=self.SERVICE_CONTENT):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        subprocess.TimeoutExpired(cmd=["sudo", "cat"], timeout=120),
                        MagicMock(returncode=1),  # cat retry: file doesn't exist
                        MagicMock(returncode=0),  # tee success
                        MagicMock(returncode=0),  # chmod success
                        MagicMock(returncode=0),  # visudo success
                    ]

                    result = executor._ensure_sudoers_restart()

        assert result is True
        assert mock_run.call_count == 5
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_sudoers_restart_retries_tee_on_timeout_then_completes_full_sequence(
        self, mock_sleep
    ):
        """The create (sudo tee) step retries once after TimeoutExpired, then
        completes the full create+chmod+visudo sequence successfully."""
        from pathlib import Path as _Path

        executor = DeploymentExecutor(
            repo_path=_Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=self.SERVICE_CONTENT):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = [
                        MagicMock(returncode=1),  # cat: file doesn't exist
                        subprocess.TimeoutExpired(cmd=["sudo", "tee"], timeout=120),
                        MagicMock(returncode=0),  # tee retry succeeds
                        MagicMock(returncode=0),  # chmod success
                        MagicMock(returncode=0),  # visudo success
                    ]

                    result = executor._ensure_sudoers_restart()

        assert result is True
        assert mock_run.call_count == 5
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_sudoers_restart_returns_false_when_step_always_times_out(
        self, mock_sleep
    ):
        """When a step's subprocess.run always raises TimeoutExpired, the
        method must fail-soft: return False without raising."""
        from pathlib import Path as _Path

        executor = DeploymentExecutor(
            repo_path=_Path("/home/user/code-indexer"),
            service_name="cidx-server",
        )

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=self.SERVICE_CONTENT):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.TimeoutExpired(
                        cmd=["sudo", "cat"], timeout=120
                    )

                    result = executor._ensure_sudoers_restart()

        assert result is False


class TestEnsureMemoryOvercommitRetry:
    """_ensure_memory_overcommit routes its sysctl-check/tee/sysctl-apply
    subprocess calls through the retry helper."""

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_memory_overcommit_retries_check_on_timeout_then_succeeds(
        self, mock_sleep
    ):
        """The sysctl -n check step retries once after TimeoutExpired, then
        reports already-configured (vm.overcommit_memory=1) and returns True
        without needing the write/apply steps."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(
                    cmd=["sysctl", "-n", "vm.overcommit_memory"], timeout=120
                ),
                MagicMock(returncode=0, stdout="1\n", stderr=""),
            ]

            result = executor._ensure_memory_overcommit()

        assert result is True
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_memory_overcommit_retries_write_on_timeout_then_succeeds(
        self, mock_sleep
    ):
        """The check reports not-configured; the sudo tee write step retries
        once after TimeoutExpired, then the sysctl -p apply step succeeds."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="0\n", stderr=""),  # check: not set
                subprocess.TimeoutExpired(
                    cmd=["sudo", "tee", "/etc/sysctl.d/99-cidx-memory.conf"],
                    timeout=120,
                ),
                MagicMock(returncode=0, stdout="", stderr=""),  # tee retry succeeds
                MagicMock(returncode=0, stdout="", stderr=""),  # sysctl -p succeeds
            ]

            result = executor._ensure_memory_overcommit()

        assert result is True
        assert mock_run.call_count == 4
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_memory_overcommit_returns_false_when_check_always_times_out(
        self, mock_sleep
    ):
        """When the sysctl -n check always times out, the method must
        fail-soft: return False (not raise) after exhausting all attempts."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["sysctl", "-n", "vm.overcommit_memory"], timeout=120
            )

            result = executor._ensure_memory_overcommit()

        assert result is False
        assert mock_run.call_count == 3  # max_attempts, retries exhausted


class TestEnsureSwapFileRetry:
    """_ensure_swap_file routes ALL its subprocess calls (check, fallocate,
    chmod, mkswap, swapon, fstab cat/tee) through the retry helper. This
    method is best-effort/non-fatal (Bug #1254): exhausted retries must
    still return True, never raise."""

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_swap_file_retries_check_on_timeout_then_succeeds(self, mock_sleep):
        """The swapon --show check retries once after TimeoutExpired, then
        reports swap already active and returns True immediately."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.TimeoutExpired(
                    cmd=["swapon", "--show", "--noheadings"], timeout=120
                ),
                MagicMock(returncode=0, stdout="/swapfile file 4G 0B -2\n", stderr=""),
            ]

            result = executor._ensure_swap_file()

        assert result is True
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_swap_file_retries_fallocate_on_timeout_then_completes_full_sequence(
        self, mock_sleep
    ):
        """No swap active yet; fallocate retries once after TimeoutExpired,
        then chmod/mkswap/swapon/fstab-check all succeed and the fstab
        already contains /swapfile (no append needed)."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),  # check: no swap
                subprocess.TimeoutExpired(
                    cmd=["sudo", "fallocate", "-l", "4G", "/swapfile"], timeout=120
                ),
                MagicMock(returncode=0, stdout="", stderr=""),  # fallocate retry ok
                MagicMock(returncode=0, stdout="", stderr=""),  # chmod ok
                MagicMock(returncode=0, stdout="", stderr=""),  # mkswap ok
                MagicMock(returncode=0, stdout="", stderr=""),  # swapon ok
                MagicMock(
                    returncode=0, stdout="/swapfile none swap sw 0 0\n", stderr=""
                ),  # fstab cat: entry already present
            ]

            result = executor._ensure_swap_file()

        assert result is True
        assert mock_run.call_count == 7
        mock_sleep.assert_called_once()

    @patch("code_indexer.server.auto_update.deployment_executor.time.sleep")
    def test_ensure_swap_file_fail_soft_when_fallocate_always_times_out(
        self, mock_sleep
    ):
        """Bug #1254: when fallocate always times out (retries exhausted),
        the method must NOT raise -- it logs and returns True (best-effort,
        swap is an OOM optimization, not a correctness requirement)."""
        executor = DeploymentExecutor(repo_path=Path("/tmp/test-repo"))

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),  # check: no swap
                subprocess.TimeoutExpired(
                    cmd=["sudo", "fallocate", "-l", "4G", "/swapfile"], timeout=120
                ),
                subprocess.TimeoutExpired(
                    cmd=["sudo", "fallocate", "-l", "4G", "/swapfile"], timeout=120
                ),
                subprocess.TimeoutExpired(
                    cmd=["sudo", "fallocate", "-l", "4G", "/swapfile"], timeout=120
                ),
            ]

            result = executor._ensure_swap_file()

        assert result is True
        assert mock_run.call_count == 4  # 1 check + 3 fallocate attempts exhausted
        assert mock_sleep.call_count == 2  # sleeps between fallocate attempts
