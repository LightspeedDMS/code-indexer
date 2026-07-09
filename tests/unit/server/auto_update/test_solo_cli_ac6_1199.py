"""
TDD tests for Story #1199 AC6: solo CLI _start_server_process appends --workers.

Behavioral: calls real _start_server_process with subprocess.Popen patched at
the boundary. No mocks of internal logic.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def manager(tmp_path: Path) -> Any:
    from code_indexer.server.lifecycle.server_lifecycle_manager import (
        ServerLifecycleManager,
    )

    return ServerLifecycleManager(server_dir=str(tmp_path))


def _run_start(manager: Any, config: dict) -> list:
    """Call _start_server_process and return the cmd list passed to Popen."""
    captured_cmds: list = []

    def fake_popen(cmd: list, **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(cmd))
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None  # None = still running (not crashed)
        return proc

    log_dir = manager.server_dir_path / "logs"
    log_dir.mkdir(exist_ok=True)

    with patch(
        "code_indexer.server.lifecycle.server_lifecycle_manager.subprocess.Popen",
        side_effect=fake_popen,
    ):
        with patch("code_indexer.server.lifecycle.server_lifecycle_manager.time.sleep"):
            manager._start_server_process(config)

    assert captured_cmds, "_start_server_process must call subprocess.Popen"
    return list(captured_cmds[0])


class TestAC6SoloCli:
    """AC6: _start_server_process must include --workers in the subprocess command."""

    def test_workers_flag_present_in_cmd(self, manager: Any, tmp_path: Path) -> None:
        """AC6 BEHAVIORAL: --workers must appear in the cmd passed to Popen."""
        config = {"host": "127.0.0.1", "port": 8000, "workers": 4}
        cmd = _run_start(manager, config)

        assert "--workers" in cmd, (
            f"AC6: --workers must be in the subprocess cmd. Got: {cmd}"
        )

    def test_workers_value_from_config(self, manager: Any, tmp_path: Path) -> None:
        """AC6 BEHAVIORAL: the workers value in cmd matches config['workers']."""
        config = {"host": "127.0.0.1", "port": 8000, "workers": 6}
        cmd = _run_start(manager, config)

        idx = cmd.index("--workers")
        assert cmd[idx + 1] == "6", (
            f"AC6: --workers value must be '6' (from config). Got: {cmd[idx + 1]!r}"
        )

    def test_host_and_port_still_present(self, manager: Any, tmp_path: Path) -> None:
        """AC6: --host and --port must still be present after adding --workers."""
        config = {"host": "0.0.0.0", "port": 9001, "workers": 2}
        cmd = _run_start(manager, config)

        assert "--host" in cmd, f"--host must still be in cmd: {cmd}"
        assert "--port" in cmd, f"--port must still be in cmd: {cmd}"
        host_val = cmd[cmd.index("--host") + 1]
        port_val = cmd[cmd.index("--port") + 1]
        assert host_val == "0.0.0.0", f"Wrong host: {host_val!r}"
        assert port_val == "9001", f"Wrong port: {port_val!r}"
