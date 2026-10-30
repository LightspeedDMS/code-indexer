"""`cidx index --exit-with-parent=<pid>` arms the parent-death exit before
anything else runs: a real CLI child, its real parent pid."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.utils.parent_death import exit_with_parent_arg

pytestmark = pytest.mark.slow

SRC_DIR = str(Path(__file__).resolve().parents[3] / "src")
CLI_BOUND_SECONDS = 120
ACCEPT_POLL_SECONDS = 0.2


def _run_index(cwd: Path, parent_pid: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "code_indexer.cli",
            "index",
            exit_with_parent_arg(parent_pid),
        ],
        cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": SRC_DIR, "HOME": str(cwd)},
        capture_output=True,
        text=True,
        timeout=CLI_BOUND_SECONDS,
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_index_exits_at_once_when_expected_parent_is_not_its_parent(tmp_path):
    # ^ why os.getppid(), not a dead pid; alive but != the child's parent => the already-gone path fires
    result = _run_index(tmp_path, parent_pid=os.getppid())
    assert result.returncode == -signal.SIGTERM, result.stderr[-2000:]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_index_with_live_parent_is_not_terminated(tmp_path):
    result = _run_index(tmp_path, parent_pid=os.getpid())
    assert result.returncode != -signal.SIGTERM, result.stderr[-2000:]
    assert "no such option" not in (result.stderr + result.stdout).lower()


class StandInDaemon:
    """A listening Unix socket at the repo's daemon socket path: anything
    that connects was trying to delegate to a daemon."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connections: List[int] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen()
        self._server.settimeout(ACCEPT_POLL_SECONDS)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections.append(1)
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(CLI_BOUND_SECONDS)
        self._server.close()
        self.path.unlink(missing_ok=True)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_server_stamped_index_never_delegates_to_a_daemon(tmp_path):
    from code_indexer.config import ConfigManager

    cidx = shutil.which("cidx")
    if cidx is None:
        pytest.skip("no installed `cidx` console script on PATH")
    repo = tmp_path / "repo"
    config_path = repo / ".code-indexer" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"daemon": {"enabled": True}}))
    daemon = StandInDaemon(Path(ConfigManager(config_path).get_socket_path()))
    try:
        result = subprocess.run(
            [cidx, "index", exit_with_parent_arg(os.getppid())],
            cwd=str(repo),
            env={**os.environ, "PYTHONPATH": SRC_DIR, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=CLI_BOUND_SECONDS,
        )
    finally:
        daemon.close()
    assert daemon.connections == [], "a server-stamped index was handed to a daemon"
    assert result.returncode == -signal.SIGTERM, (
        result.stdout[-2000:] + result.stderr[-2000:]
    )
