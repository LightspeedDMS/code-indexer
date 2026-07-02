"""Tests for Bug #879 fix: CIDX_DATA_DIR env var for IPC path resolution.

Three module-level constants in deployment_executor.py use Path.home() at
import time. When cidx-server runs as User=code-indexer (HOME=/opt/code-indexer)
and cidx-auto-update runs as User=root (HOME=/root), the paths diverge and
break the inter-process file contract (restart signal, redeploy marker, status
file). The fix: honor CIDX_DATA_DIR env var, fall back to Path.home()/.cidx-server.
"""

import importlib
import os
import pwd
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def executor():
    """Create DeploymentExecutor instance for testing."""
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


@pytest.fixture
def current_user_info():
    """Return (username, home_path, server_service_content) for the running user.

    Uses real pwd lookups so the test exercises the same code path as
    _ensure_data_dir_env_var() in production.
    """
    username = pwd.getpwuid(os.getuid()).pw_name
    home = Path(pwd.getpwnam(username).pw_dir)
    service_content = _SERVER_SERVICE_WITH_USER.replace(
        "User=code-indexer", f"User={username}"
    )
    return username, home, service_content


# ---------------------------------------------------------------------------
# Service file templates
# ---------------------------------------------------------------------------

_SERVER_SERVICE_NO_USER = """\
[Unit]
Description=CIDX Server

[Service]
ExecStart=/usr/bin/python3 -m uvicorn code_indexer.server.app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

_SERVER_SERVICE_WITH_USER = """\
[Unit]
Description=CIDX Server

[Service]
User=code-indexer
WorkingDirectory=/opt/code-indexer
ExecStart=/opt/pipx/venvs/code-indexer/bin/python -m uvicorn code_indexer.server.app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

_AUTO_UPDATE_SERVICE_NO_DATA_DIR = """\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
Environment="CIDX_REPO_ROOT=/opt/code-indexer"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""

_AUTO_UPDATE_SERVICE_WITH_STALE_DATA_DIR = """\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
Environment="CIDX_REPO_ROOT=/opt/code-indexer"
Environment="CIDX_DATA_DIR=/old/wrong/path/.cidx-server"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""


def _auto_update_service_with_correct_data_dir(data_dir: str) -> str:
    """Build auto-update service content with CIDX_DATA_DIR inside [Service] block."""
    return f"""\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
Environment="CIDX_REPO_ROOT=/opt/code-indexer"
Environment="CIDX_DATA_DIR={data_dir}"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""


def _make_subprocess_run_mock(*service_contents):
    """Build a side_effect list for subprocess.run.

    Each item in service_contents maps to one 'sudo cat' call.
    Subsequent calls (sudo tee, systemctl daemon-reload, systemctl restart)
    return success by default.
    """
    read_responses = [
        Mock(returncode=0, stdout=content, stderr="") for content in service_contents
    ]
    write_success = Mock(returncode=0, stdout="", stderr="")
    return read_responses + [write_success, write_success, write_success]


# ---------------------------------------------------------------------------
# Tests 1-2: Module-level constant resolution
# ---------------------------------------------------------------------------


class TestModuleConstantsEnvVar:
    """Tests for CIDX_DATA_DIR env var honoring in module constants."""

    def test_constants_resolve_from_cidx_data_dir_env_var(self, monkeypatch, tmp_path):
        """Test 1: Module constants use CIDX_DATA_DIR when env var is set."""
        custom_dir = str(tmp_path / "custom-cidx")
        monkeypatch.setenv("CIDX_DATA_DIR", custom_dir)

        import code_indexer.server.auto_update.deployment_executor as dx

        importlib.reload(dx)

        assert str(dx.RESTART_SIGNAL_PATH).startswith(custom_dir), (
            f"RESTART_SIGNAL_PATH={dx.RESTART_SIGNAL_PATH} should start with {custom_dir}"
        )
        assert str(dx.PENDING_REDEPLOY_MARKER).startswith(custom_dir), (
            f"PENDING_REDEPLOY_MARKER={dx.PENDING_REDEPLOY_MARKER} should start with {custom_dir}"
        )
        assert str(dx.AUTO_UPDATE_STATUS_FILE).startswith(custom_dir), (
            f"AUTO_UPDATE_STATUS_FILE={dx.AUTO_UPDATE_STATUS_FILE} should start with {custom_dir}"
        )

        assert dx.RESTART_SIGNAL_PATH.name == "restart.signal"
        assert dx.PENDING_REDEPLOY_MARKER.name == "pending-redeploy"
        assert dx.AUTO_UPDATE_STATUS_FILE.name == "auto-update-status.json"

    def test_constants_fall_back_to_home_when_env_var_unset(self, monkeypatch):
        """Test 2: Module constants fall back to Path.home()/.cidx-server when CIDX_DATA_DIR is unset."""
        monkeypatch.delenv("CIDX_DATA_DIR", raising=False)

        import code_indexer.server.auto_update.deployment_executor as dx

        importlib.reload(dx)

        expected_base = str(Path.home() / ".cidx-server")

        assert str(dx.RESTART_SIGNAL_PATH).startswith(expected_base), (
            f"RESTART_SIGNAL_PATH={dx.RESTART_SIGNAL_PATH} should start with {expected_base}"
        )
        assert str(dx.PENDING_REDEPLOY_MARKER).startswith(expected_base), (
            f"PENDING_REDEPLOY_MARKER={dx.PENDING_REDEPLOY_MARKER} should start with {expected_base}"
        )
        assert str(dx.AUTO_UPDATE_STATUS_FILE).startswith(expected_base), (
            f"AUTO_UPDATE_STATUS_FILE={dx.AUTO_UPDATE_STATUS_FILE} should start with {expected_base}"
        )


# ---------------------------------------------------------------------------
# Tests 3-9: _ensure_data_dir_env_var() method
# ---------------------------------------------------------------------------


class TestEnsureDataDirEnvVar:
    """Tests for DeploymentExecutor._ensure_data_dir_env_var() method.

    subprocess.run is the true external boundary: _read_service_file,
    _write_service_file_and_reload, and _restart_auto_update_service all
    delegate to subprocess.run. Patching it exercises the real method bodies.
    """

    def test_returns_true_no_write_when_same_user_no_user_line(self, executor):
        """Test 3: Returns True and does NOT write service file when no User= in server service.

        When server runs without a User= directive, both processes run as the
        same user, so Path.home() is identical — no mismatch possible.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                _SERVER_SERVICE_NO_USER,
                _AUTO_UPDATE_SERVICE_NO_DATA_DIR,
            )
            result = executor._ensure_data_dir_env_var()

        assert result is True
        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(tee_calls) == 0, "sudo tee should not be called for same-user case"
        assert len(restart_calls) == 0, (
            "systemctl restart should not be called for same-user case"
        )

    def test_patches_service_file_when_different_user_and_no_data_dir(
        self, executor, current_user_info
    ):
        """Test 4: Patches auto-updater service when server has User= and no CIDX_DATA_DIR set.

        This is the primary injection path.
        """
        _, user_home, server_service = current_user_info
        expected_data_dir = str(user_home / ".cidx-server")
        expected_env_line = f'Environment="CIDX_DATA_DIR={expected_data_dir}"'

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                server_service,
                _AUTO_UPDATE_SERVICE_NO_DATA_DIR,
            )
            result = executor._ensure_data_dir_env_var()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 1, "sudo tee should be called exactly once"

        written_content = tee_calls[0][1]["input"]
        assert expected_env_line in written_content, (
            f"Written content should contain {expected_env_line}"
        )

        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(restart_calls) == 1, (
            "systemctl restart should be called exactly once"
        )

    def test_idempotent_no_write_when_correct_data_dir_already_present(
        self, executor, current_user_info
    ):
        """Test 5: Returns True, no write/restart when correct CIDX_DATA_DIR already present inside [Service]."""
        _, user_home, server_service = current_user_info
        correct_data_dir = str(user_home / ".cidx-server")
        auto_update_already_correct = _auto_update_service_with_correct_data_dir(
            correct_data_dir
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                server_service,
                auto_update_already_correct,
            )
            result = executor._ensure_data_dir_env_var()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 0, "sudo tee should NOT be called when already correct"

        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(restart_calls) == 0, (
            "systemctl restart should NOT be called when already correct"
        )

    def test_preserves_existing_env_lines_and_inserts_after_last(
        self, executor, current_user_info
    ):
        """Test 6: Preserves existing Environment= lines and inserts new line after the last one."""
        _, user_home, server_service = current_user_info

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                server_service,
                _AUTO_UPDATE_SERVICE_NO_DATA_DIR,
            )
            result = executor._ensure_data_dir_env_var()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        written_content = tee_calls[0][1]["input"]
        lines = written_content.splitlines()

        assert any("CIDX_REPO_ROOT" in line for line in lines), (
            "CIDX_REPO_ROOT should be preserved"
        )
        assert any("CIDX_DATA_DIR" in line for line in lines), (
            "CIDX_DATA_DIR should be inserted"
        )

        repo_root_idx = next(
            i for i, line in enumerate(lines) if "CIDX_REPO_ROOT" in line
        )
        data_dir_idx = next(
            i for i, line in enumerate(lines) if "CIDX_DATA_DIR" in line
        )
        assert data_dir_idx > repo_root_idx, (
            "CIDX_DATA_DIR should appear after CIDX_REPO_ROOT"
        )

    def test_strips_stale_data_dir_and_inserts_fresh(self, executor, current_user_info):
        """Test 7: Strips stale CIDX_DATA_DIR= line (wrong path) and inserts correct one."""
        _, user_home, server_service = current_user_info
        correct_data_dir = str(user_home / ".cidx-server")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                server_service,
                _AUTO_UPDATE_SERVICE_WITH_STALE_DATA_DIR,
            )
            result = executor._ensure_data_dir_env_var()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        written_content = tee_calls[0][1]["input"]

        assert "/old/wrong/path" not in written_content, "Stale path must be removed"
        assert correct_data_dir in written_content, "Correct path must be present"
        assert written_content.count("CIDX_DATA_DIR") == 1, (
            "CIDX_DATA_DIR should appear exactly once"
        )

    def test_inserts_data_dir_when_no_existing_env_lines(
        self, executor, current_user_info
    ):
        """Test edge case: auto-update service has no Environment= lines at all.

        When last_env_index stays -1, CIDX_DATA_DIR must still be inserted
        at a deterministic location (e.g., after [Service] header).
        """
        _, user_home, server_service = current_user_info
        expected_data_dir = str(user_home / ".cidx-server")
        expected_env_line = f'Environment="CIDX_DATA_DIR={expected_data_dir}"'

        auto_update_no_env = """\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                server_service,
                auto_update_no_env,
            )
            result = executor._ensure_data_dir_env_var()

        assert result is True
        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 1, "sudo tee should be called once"
        written_content = tee_calls[0][1]["input"]
        assert expected_env_line in written_content, (
            "CIDX_DATA_DIR line must be present even with no prior Environment= lines"
        )

    def test_returns_false_when_restart_auto_update_fails(
        self, executor, current_user_info
    ):
        """Test 11: Returns False when systemctl restart of auto-updater fails.

        Catches silent-failure mode where service file is patched correctly but
        systemd restart fails — without propagation, CIDX_DATA_DIR would never
        take effect in the running process.
        """
        _, _, server_service = current_user_info

        with patch("subprocess.run") as mock_run:
            # 1: sudo cat server service      -> success
            # 2: sudo cat auto-update service  -> success (no CIDX_DATA_DIR yet)
            # 3: sudo tee auto-update service  -> success
            # 4: sudo systemctl daemon-reload  -> success
            # 5: sudo systemctl restart        -> FAILURE
            mock_run.side_effect = [
                Mock(returncode=0, stdout=server_service, stderr=""),
                Mock(returncode=0, stdout=_AUTO_UPDATE_SERVICE_NO_DATA_DIR, stderr=""),
                Mock(returncode=0, stdout="", stderr=""),  # tee
                Mock(returncode=0, stdout="", stderr=""),  # daemon-reload
                Mock(returncode=1, stdout="", stderr="restart failed"),  # restart fails
            ]
            result = executor._ensure_data_dir_env_var()

        assert result is False, (
            "Must return False when restart fails so DEPLOY-GENERAL-058 fires "
            "at the execute() level — otherwise CIDX_DATA_DIR never takes effect."
        )

    def test_returns_true_when_server_service_file_absent(self, executor):
        """Test 8: Returns True (non-fatal) when server service file does not exist (fresh install)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="", stderr="No such file")
            result = executor._ensure_data_dir_env_var()

        assert result is True
        assert mock_run.call_count == 1, (
            "Should only make one subprocess call (server service read)"
        )

    def test_returns_false_when_auto_update_service_read_fails(
        self, executor, current_user_info
    ):
        """Test 9: Returns False when auto-updater service file read fails."""
        _, _, server_service = current_user_info

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout=server_service, stderr=""),
                Mock(returncode=1, stdout="", stderr="Permission denied"),
            ]
            result = executor._ensure_data_dir_env_var()

        assert result is False

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 0, "sudo tee should NOT be called when read fails"


# ---------------------------------------------------------------------------
# Test 10: execute() wiring order
# ---------------------------------------------------------------------------


class TestExecuteWiring:
    """Verify _ensure_data_dir_env_var() is wired into execute() at Step 6.5."""

    def test_execute_calls_ensure_data_dir_env_var_between_python_and_ripgrep(
        self, executor
    ):
        """Test 10: execute() invokes _ensure_data_dir_env_var() at Step 6.5
        (between _ensure_auto_updater_uses_server_python and ensure_ripgrep).

        Uses source inspection rather than runtime patching so that the test
        remains fast, deterministic, and independent of DeploymentExecutor's
        many other collaborators that execute() invokes.
        """
        import inspect

        from code_indexer.server.auto_update.deployment_executor import (
            DeploymentExecutor,
        )

        source = inspect.getsource(DeploymentExecutor.execute)
        python_idx = source.index("_ensure_auto_updater_uses_server_python")
        data_dir_idx = source.index("_ensure_data_dir_env_var")
        ripgrep_idx = source.index("ensure_ripgrep")
        assert python_idx < data_dir_idx < ripgrep_idx, (
            "execute() must call _ensure_data_dir_env_var() after "
            "_ensure_auto_updater_uses_server_python() and before ensure_ripgrep()"
        )
