"""Once the Web UI has committed the cidx-meta backup settings, the response
says they were saved. A bootstrap push that then fails is reported as a
separate warning naming a neutral reason -- the remote stalled, is
unreachable, or rejected the push -- with a non-5xx status; a stall is
never called a rejection, and neither the remote URL nor git's raw output
reaches the page. The catalog lock is released and the refresh, whose
backup phase retries the bootstrap, is owed.

Real components: the real route and save path, a real file WriteLockManager,
the real bootstrap running real git against a real rejecting origin, a
missing origin and a real loopback HTTP server that accepts and then sends
nothing. The stall window is shortened (the detector's own setting) so the
stall is detected in about a second.
"""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.services.cidx_meta_backup import stall_detection
from tests.unit.server.web.test_cidx_meta_backup_save_holds_backup_lock import (
    CATALOG,
    _client,
    world,
)

__all__ = ["world"]
SAVED = "cidx-meta backup configuration saved"
STALL_SECONDS = 2.0


def _post(client: Any, remote_url: str) -> Any:
    return client.post(
        "/admin/config/cidx_meta_backup",
        data={"enabled": "true", "remote_url": remote_url, "csrf_token": "x"},
    )


def _assert_saved_with_warning(
    response: Any, world: Dict[str, Any], reason: str, remote_url: str
) -> None:
    assert response.status_code == 200, response.text
    assert response.text.startswith(SAVED), response.text
    assert f"the backup push failed: {reason}" in response.text, response.text
    assert remote_url not in response.text, "the remote URL reached the page"
    assert "fatal:" not in response.text, "git's raw output reached the page"
    assert world["config"].saved, "the settings were not saved"
    assert world["locks"].get_lock_info(CATALOG) is None
    assert world["debouncer"].signals == 1, "the retrying refresh was not owed"


@pytest.fixture
def stalling_remote() -> Iterator[str]:
    """A loopback HTTP endpoint that accepts, reads, and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    server.settimeout(0.2)
    done = threading.Event()
    held: List[socket.socket] = []

    def _serve() -> None:
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            held.append(conn)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}/example-repo.git"
    finally:
        done.set()
        thread.join(timeout=5)
        for conn in held:
            conn.close()
        server.close()


def test_a_stalled_push_is_reported_as_a_stall_after_saving(
    monkeypatch: pytest.MonkeyPatch, world: Dict[str, Any], stalling_remote: str
) -> None:
    monkeypatch.setattr(stall_detection, "HTTP_LOW_SPEED_TIME_SECONDS", 1)
    client = _client(monkeypatch, world["config"], world["locks"])

    response = _post(client, stalling_remote)

    _assert_saved_with_warning(response, world, "the remote stalled", stalling_remote)
    assert "rejected" not in response.text, "a stall was called a rejection"


def test_an_unreachable_remote_is_reported_after_saving(
    monkeypatch: pytest.MonkeyPatch, world: Dict[str, Any], tmp_path: Path
) -> None:
    missing = (tmp_path / "no-such-origin.git").as_uri()
    client = _client(monkeypatch, world["config"], world["locks"])

    response = _post(client, missing)

    _assert_saved_with_warning(response, world, "the remote is unreachable", missing)


def test_a_rejected_push_is_reported_after_saving(
    monkeypatch: pytest.MonkeyPatch, world: Dict[str, Any]
) -> None:
    origin: Path = world["origin"]
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'example policy refuses this push' >&2\nexit 1\n")
    hook.chmod(0o755)
    client = _client(monkeypatch, world["config"], world["locks"])

    response = _post(client, origin.as_uri())

    _assert_saved_with_warning(
        response, world, "the remote rejected the push", origin.as_uri()
    )
    log = subprocess.run(
        ["git", "log", "--all", "--oneline"], cwd=origin, capture_output=True, text=True
    )
    assert log.stdout.strip() == ""
