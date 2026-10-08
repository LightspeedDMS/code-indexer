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
from typing import Any, Callable, Dict, Iterator, List

import pytest
import rpyc
from rpyc.utils.server import ThreadedServer

from code_indexer import cli_daemon_delegation

FAILURE_MESSAGE = "embedding provider unavailable"
SERVER_READY_TIMEOUT_SECONDS = 5.0
SERVER_READY_POLL_SECONDS = 0.02
SERVER_JOIN_TIMEOUT_SECONDS = 5.0


class _QueryService(rpyc.Service):
    """Answers every daemon call used here with a fixed response."""

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response

    def _answer(self) -> Dict[str, Any]:
        return dict(self._response)

    def exposed_query(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._answer()

    def exposed_clean(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._answer()

    def exposed_clean_data(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._answer()

    def exposed_watch_stop(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._answer()

    def exposed_watch_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self._answer()


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


# A real RPyC result is a netref: reading it, or handing it to a caller, after
# ``conn.close()`` raises "stream has been closed". clean and clean-data then
# fell back to standalone after the daemon had already done the work.

CLEAN_DAEMON_RESPONSE: Dict[str, Any] = {"status": "success", "cache_invalidated": True}
WATCH_STOP_DAEMON_RESPONSE: Dict[str, Any] = {
    "status": "success",
    "files_processed": 3,
    "updates_applied": 2,
}
WATCH_STATUS_DAEMON_RESPONSE: Dict[str, Any] = {
    "running": True,
    "project_path": "/example/project",
    "stats": {"files_processed": 3, "changes": ["a.py", "b.py"]},
}

CLEAN_COMMANDS: Dict[str, Callable[[], int]] = {
    "clean": lambda: cli_daemon_delegation._clean_via_daemon(),
    "clean-data": lambda: cli_daemon_delegation._clean_data_via_daemon(),
}
WATCH_COMMANDS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "watch-stop": lambda: cli_daemon_delegation.stop_watch_via_daemon(Path.cwd()),
    "watch-status": lambda: cli_daemon_delegation.get_watch_status_via_daemon(
        Path.cwd()
    ),
}


def _assert_no_recovery(output: str, spawned: List[Path], socket_kept: bool) -> None:
    assert "stream has been closed" not in output, output
    assert "Falling back" not in output, output
    assert "attempting restart" not in output, output
    assert spawned == [], "an answered daemon call must never restart the daemon"
    assert socket_kept, "the live daemon's socket was unlinked"


@pytest.mark.parametrize("command", sorted(CLEAN_COMMANDS))
def test_clean_commands_report_daemon_result(
    project: Path,
    spawned: List[Path],
    capsys: pytest.CaptureFixture,
    command: str,
) -> None:
    socket_path = cli_daemon_delegation._get_socket_path(project)
    with live_daemon_socket(socket_path, CLEAN_DAEMON_RESPONSE):
        exit_code = CLEAN_COMMANDS[command]()
        socket_kept = socket_path.exists()

    output = capsys.readouterr().out
    assert exit_code == 0, output
    assert "Cache invalidated: True" in output, output
    _assert_no_recovery(output, spawned, socket_kept)


@pytest.mark.parametrize(
    "command, daemon_response",
    [
        ("watch-stop", WATCH_STOP_DAEMON_RESPONSE),
        ("watch-status", WATCH_STATUS_DAEMON_RESPONSE),
    ],
)
def test_watch_commands_return_plain_dict(
    project: Path,
    spawned: List[Path],
    capsys: pytest.CaptureFixture,
    command: str,
    daemon_response: Dict[str, Any],
) -> None:
    socket_path = cli_daemon_delegation._get_socket_path(project)
    with live_daemon_socket(socket_path, daemon_response):
        returned = WATCH_COMMANDS[command]()
        socket_kept = socket_path.exists()

    output = capsys.readouterr().out
    assert type(returned) is dict, type(returned)
    assert returned == daemon_response
    for key, value in daemon_response.items():
        assert type(returned[key]) is type(value), key
    _assert_no_recovery(output, spawned, socket_kept)


def test_daemon_result_copy_never_unpickles(
    project: Path, spawned: List[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever owns the socket must never get the CLI to unpickle its data."""
    import pickle

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("daemon results must never go through pickle")

    monkeypatch.setattr(pickle, "loads", _forbidden)
    monkeypatch.setattr(pickle, "dumps", _forbidden)
    socket_path = cli_daemon_delegation._get_socket_path(project)
    with live_daemon_socket(socket_path, WATCH_STATUS_DAEMON_RESPONSE):
        returned = cli_daemon_delegation.get_watch_status_via_daemon(Path.cwd())

    assert returned == WATCH_STATUS_DAEMON_RESPONSE
    assert type(returned["stats"]) is dict
    assert type(returned["stats"]["changes"]) is list


@pytest.mark.parametrize(
    "value", [object(), {1, 2}, {"nested": [frozenset({1})]}], ids=repr
)
def test_obtain_daemon_result_rejects_unexpected_type(value: Any) -> None:
    with pytest.raises(TypeError, match="unexpected daemon result type"):
        cli_daemon_delegation.obtain_daemon_result(value)


def test_obtain_daemon_result_converts_tuples_and_keeps_scalars() -> None:
    value = {"a": (1, 2.5, None), "b": [True, b"x", "s"]}
    assert cli_daemon_delegation.obtain_daemon_result(value) == {
        "a": [1, 2.5, None],
        "b": [True, b"x", "s"],
    }


def test_obtain_daemon_result_rejects_excess_depth() -> None:
    within: Any = "leaf"
    for _ in range(cli_daemon_delegation.DAEMON_RESULT_MAX_DEPTH):
        within = [within]
    assert cli_daemon_delegation.obtain_daemon_result(within) == within

    too_deep = [within]
    with pytest.raises(ValueError, match="nested deeper"):
        cli_daemon_delegation.obtain_daemon_result(too_deep)


def test_obtain_daemon_result_rejects_excess_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_daemon_delegation, "DAEMON_RESULT_MAX_ITEMS", 4)
    assert cli_daemon_delegation.obtain_daemon_result([1, 2, 3]) == [1, 2, 3]
    with pytest.raises(ValueError, match="more than 4 values"):
        cli_daemon_delegation.obtain_daemon_result([1, 2, 3, 4])


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
