"""A completed daemon query never orphans the live daemon.

Regression (e2e Phase 2, ``cidx watch`` after ``cidx query`` in daemon mode):
the full-CLI delegation path read the RPyC result AFTER closing the
connection. A real RPyC result is a netref, so that read raised
"stream has been closed"; the crash-recovery branch then treated the
already-answered query as a dead daemon, unlinked the LIVE daemon's socket and
spawned a replacement that cannot start (the live daemon holds the
index-mutation lock, Story #1488). The daemon was left serving on an
unreachable socket while still holding the lock, so a later ``cidx watch``
could not reach it, fell back to standalone and failed closed on the lock.

These tests run a REAL RPyC server on the daemon's socket path so the result
crosses a real connection as a netref. Only the daemon process spawn
(``_start_daemon``) is replaced, to record whether recovery was attempted.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
import rpyc
from rpyc.utils.server import ThreadedServer

from code_indexer import cli_daemon_delegation

FAILURE_MESSAGE = "embedding provider unavailable"
SERVER_READY_TIMEOUT_SECONDS = 5.0
SERVER_READY_POLL_SECONDS = 0.02
SERVER_JOIN_TIMEOUT_SECONDS = 5.0


class _QueryService(rpyc.Service):
    """Answers exposed_query with a fixed response, like the daemon does."""

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response

    def exposed_query(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return dict(self._response)


@contextlib.contextmanager
def live_daemon_socket(socket_path: Path, response: Dict[str, Any]) -> Iterator[None]:
    """Serve ``response`` on ``socket_path`` with a real RPyC server."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    server = ThreadedServer(
        _QueryService(response),
        socket_path=str(socket_path),
        protocol_config={"allow_public_attrs": True, "allow_pickle": True},
    )
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_READY_TIMEOUT_SECONDS
    while not server.active and time.monotonic() < deadline:
        time.sleep(SERVER_READY_POLL_SECONDS)
    assert server.active, "test RPyC server never started listening"
    try:
        yield
    finally:
        server.close()
        thread.join(timeout=SERVER_JOIN_TIMEOUT_SECONDS)
        if socket_path.exists():
            socket_path.unlink()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({"codebase_dir": str(tmp_path)}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_daemon_delegation, "_find_config_file", lambda: config_path)
    return config_path


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> List[Path]:
    calls: List[Path] = []
    monkeypatch.setattr(
        cli_daemon_delegation, "_start_daemon", lambda path: calls.append(path)
    )
    return calls


@pytest.mark.parametrize(
    "response, expected_exit",
    [
        ({"results": [], "timing": {}}, 0),
        ({"results": [], "timing": {}, "error": FAILURE_MESSAGE}, 1),
    ],
    ids=["success", "failed-search"],
)
def test_answered_query_keeps_live_daemon_socket(
    project: Path,
    spawned: List[Path],
    capsys: pytest.CaptureFixture,
    response: Dict[str, Any],
    expected_exit: int,
) -> None:
    socket_path = cli_daemon_delegation._get_socket_path(project)
    with live_daemon_socket(socket_path, response):
        exit_code = cli_daemon_delegation._query_via_daemon(
            "find the thing", {"retry_delays_ms": [50]}, limit=5
        )
        socket_still_present = socket_path.exists()

    output = capsys.readouterr().out
    assert exit_code == expected_exit, output
    assert "attempting restart" not in output, output
    assert spawned == [], "an answered query must never restart the daemon"
    assert socket_still_present, "the live daemon's socket was unlinked"


def test_cleanup_keeps_socket_a_daemon_is_listening_on(project: Path) -> None:
    """Crash recovery must never unlink a LIVE daemon's socket.

    The live daemon holds the index-mutation lock for its lifetime, so a
    replacement can never start: unlinking its socket only orphans it.
    """
    socket_path = cli_daemon_delegation._get_socket_path(project)
    with live_daemon_socket(socket_path, {"results": []}):
        cli_daemon_delegation._cleanup_stale_socket(socket_path)
        socket_still_present = socket_path.exists()

    assert socket_still_present, "the live daemon's socket was unlinked"


def test_cleanup_removes_socket_nobody_listens_on(tmp_path: Path) -> None:
    import socket as socket_module

    socket_path = tmp_path / "d.sock"
    stale = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()  # file stays, nothing listens: a dead daemon's leftover
    assert socket_path.exists()

    cli_daemon_delegation._cleanup_stale_socket(socket_path)

    assert not socket_path.exists()
