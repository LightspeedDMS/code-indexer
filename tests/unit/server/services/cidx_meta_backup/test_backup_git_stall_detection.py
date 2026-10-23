"""The catalog backup's network git calls detect a stalled remote instead
of blocking forever (the exclusive catalog lock is held across them), with
no wall-clock timeout: an HTTP(S) transfer slower than the low-speed limit
for the low-speed time is abandoned, and SSH sends keepalives appended to
the existing non-interactive ssh command.

Real git against a real local endpoint (127.0.0.1, ephemeral port) that
accepts connections and never answers; the stall constants are shortened
so the test is quick. The exact invocation is checked by wrapping the real
subprocess runner.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest

from code_indexer.server.services.cidx_meta_backup import bootstrap as bootstrap_mod
from code_indexer.server.services.cidx_meta_backup import stall_detection
from code_indexer.server.services.cidx_meta_backup import sync as sync_mod
from code_indexer.server.services.cidx_meta_backup.bootstrap import (
    CidxMetaBackupBootstrap,
)
from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

WAIT_SECONDS = 60.0
KEEPALIVE = "-o ServerAliveInterval=15 -o ServerAliveCountMax=4"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("cidx-meta\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def stalling_endpoint() -> Iterator[str]:
    """An HTTP endpoint that accepts, reads the request and never answers."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: List[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except (socket.timeout, OSError):
                continue
            held.append(conn)

    acceptor = threading.Thread(target=_accept, daemon=True)
    acceptor.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/cidx-meta.git"
    finally:
        stop.set()
        acceptor.join(WAIT_SECONDS)
        for conn in held:
            conn.close()
        server.close()


def test_a_stalled_remote_fails_the_sync_instead_of_hanging(
    tmp_path: Path, stalling_endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stall_detection, "HTTP_LOW_SPEED_LIMIT_BYTES", 1)
    monkeypatch.setattr(stall_detection, "HTTP_LOW_SPEED_TIME_SECONDS", 2)
    repo = _repo(tmp_path / "cidx-meta")
    _git(repo, "remote", "add", "origin", stalling_endpoint)
    (repo / "example-repo.md").write_text("# example-repo\n")
    outcome: List[Any] = []

    worker = threading.Thread(
        target=lambda: outcome.append(CidxMetaBackupSync(str(repo), "master").sync()),
        daemon=True,
    )
    worker.start()
    worker.join(WAIT_SECONDS)

    assert not worker.is_alive(), "the backup sync blocked on a stalled remote"
    assert outcome and outcome[0].sync_failure, outcome


def _record_runs(
    monkeypatch: pytest.MonkeyPatch, module: Any
) -> List[Tuple[List[str], str]]:
    real = module.run_with_cancel
    calls: List[Tuple[List[str], str]] = []

    def _recording(argv: List[str], cancel_check: Any, **kwargs: Any) -> Any:
        calls.append((list(argv), str(kwargs["env"].get("GIT_SSH_COMMAND", ""))))
        return real(argv, cancel_check, **kwargs)

    monkeypatch.setattr(module, "run_with_cancel", _recording)
    return calls


def _assert_stall_detecting(calls: List[Tuple[List[str], str]], verb: str) -> None:
    network = [(argv, ssh) for argv, ssh in calls if verb in argv]
    assert network, f"no git {verb} ran"
    for argv, ssh in network:
        assert argv[:5] == [
            "git",
            "-c",
            "http.lowSpeedLimit=1000",
            "-c",
            "http.lowSpeedTime=60",
        ], argv
        assert ssh.startswith("ssh -o BatchMode=yes"), ssh
        assert ssh.endswith(KEEPALIVE), ssh


def test_sync_fetch_and_push_detect_a_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = _repo(tmp_path / "cidx-meta")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "master")
    (repo / "example-repo.md").write_text("# example-repo\n")
    calls = _record_runs(monkeypatch, sync_mod)

    result = CidxMetaBackupSync(str(repo), "master").sync()

    assert result.sync_failure is None, result
    _assert_stall_detecting(calls, "fetch")
    _assert_stall_detecting(calls, "push")


def test_bootstrap_push_detects_a_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    meta = tmp_path / "cidx-meta"
    meta.mkdir()
    (meta / "example-repo.md").write_text("# example-repo\n")
    calls = _record_runs(monkeypatch, bootstrap_mod)

    CidxMetaBackupBootstrap().bootstrap(str(meta), str(origin))

    _assert_stall_detecting(calls, "push")
