"""
Regression tests for Bug #1513 — CowDaemonBackend HTTP calls have no per-request
timeout, so a lost/dropped response from the CoW Storage Daemon hangs the caller
forever instead of failing loudly.

Production symptom (staging cluster, reproduced twice on a large golden repo
"evolution"): repository activation stuck at progress=40% ("Cloning repository")
indefinitely. cidx-server's log showed `CowDaemonBackend: creating clone at
path ...` with no follow-up line ever. `ss -tnp` showed the HTTP connection to
the CoW daemon stuck in CLOSE-WAIT — the daemon side closed the connection, but
cidx-server's client (the `requests` library call) never returned because it
had no read timeout at all.

Mocking policy (anti-mock culture): the primary test below uses a REAL local
TCP "black hole" server (accepts the connection, reads the request, and never
sends a response) rather than a mocked `requests` module — this proves the fix
at the actual socket/timeout layer, matching the real CLOSE-WAIT symptom, not
merely that a `timeout=` kwarg string was added to a mock call. A secondary,
fast mock-based test additionally pins down that EVERY `requests.*` call site
in CowDaemonBackend passes an explicit timeout (covers delete/list/exists too,
which are impractical to black-hole-test individually without slowing the
suite down for no extra confidence).
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


class _BlackHoleServer:
    """A real TCP server that accepts connections and never responds.

    Simulates the production CLOSE-WAIT symptom: the daemon's response is
    lost/dropped, so a client with no read timeout blocks forever waiting
    for bytes that will never arrive.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            # Accept the connection and read whatever the client sends, but
            # deliberately NEVER send a response and NEVER close the socket.
            try:
                conn.settimeout(5.0)
                conn.recv(65536)
            except OSError:
                pass
            # Intentionally fall through without responding or closing --
            # this is the "lost/dropped response" condition from Bug #1513.

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def black_hole_daemon():
    server = _BlackHoleServer()
    yield server
    server.stop()


def _make_backend(
    black_hole_daemon: _BlackHoleServer, request_timeout_seconds: Optional[int] = None
):
    from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
    from code_indexer.server.utils.config_manager import CowDaemonConfig

    kwargs = dict(
        daemon_url=f"http://127.0.0.1:{black_hole_daemon.port}",
        api_key="test-key",
        mount_point="/mnt/nfs/cidx",
        poll_interval_seconds=1,
        # NOTE: timeout_seconds bounds the overall job-completion poll loop
        # (Bug #1513 is NOT about that deadline). Set generously large here so
        # a GREEN pass is unambiguously due to the new per-request timeout,
        # not this unrelated overall deadline.
        timeout_seconds=600,
        daemon_storage_path="/mnt/nfs/cidx",
    )
    if request_timeout_seconds is not None:
        kwargs["request_timeout_seconds"] = request_timeout_seconds
    config = CowDaemonConfig(**kwargs)
    return CowDaemonBackend(config=config, visibility_waiter=lambda _p: None)


class TestCowDaemonBackendCreateClonePostIsBounded:
    """Bug #1513: the create-clone POST call must be bounded, never hang forever."""

    def test_create_clone_at_path_post_call_is_bounded_not_infinite_hang(
        self, black_hole_daemon
    ):
        """
        Against a real socket that accepts the connection but never responds,
        create_clone_at_path's POST call must fail within a small bounded
        window, not hang indefinitely.

        This exercises the exact production defect: CowDaemonConfig gains a
        `request_timeout_seconds` field (this test fails fast with a TypeError
        against pre-fix code, since that field/timeout plumbing does not yet
        exist) and CowDaemonBackend must apply it as a per-HTTP-call timeout.
        """
        backend = _make_backend(black_hole_daemon, request_timeout_seconds=2)

        result: dict = {}

        def _call() -> None:
            try:
                backend.create_clone_at_path(
                    "/mnt/nfs/cidx/source", "/mnt/nfs/cidx/.versioned/ns/name"
                )
            except Exception as exc:  # noqa: BLE001 -- capturing for assertion
                result["exception"] = exc
            else:
                result["exception"] = None

        thread = threading.Thread(target=_call, daemon=True)
        start = time.monotonic()
        thread.start()
        # Bounded join: generous headroom (10s) over the configured 2s
        # per-request timeout -- proves the call does NOT hang forever.
        thread.join(timeout=10)
        elapsed = time.monotonic() - start

        assert not thread.is_alive(), (
            f"create_clone_at_path did not return within {elapsed:.1f}s against "
            "an unresponsive daemon -- the HTTP call has no bound (Bug #1513)."
        )
        assert result.get("exception") is not None, (
            "create_clone_at_path must raise a loud error when the daemon "
            "connection is lost/unresponsive, not silently hang or succeed."
        )


class TestCowDaemonBackendAllRequestsCallsHaveTimeout:
    """Bug #1513: every requests.* call site in CowDaemonBackend must pass timeout=."""

    def _make_mocked_backend(self):
        from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
        from code_indexer.server.utils.config_manager import CowDaemonConfig

        config = CowDaemonConfig(
            daemon_url="http://daemon:8081",
            api_key="test-api-key",
            mount_point="/mnt/nfs/cidx",
            poll_interval_seconds=1,
            timeout_seconds=30,
            daemon_storage_path="/mnt/nfs/cidx",
            request_timeout_seconds=5,
        )
        return CowDaemonBackend(config=config, visibility_waiter=lambda _p: None)

    def test_create_clone_post_passes_timeout_kwarg(self):
        backend = self._make_mocked_backend()
        mock_req = MagicMock()
        mock_req.post.return_value = MagicMock(
            status_code=202, json=MagicMock(return_value={"job_id": "j1"})
        )
        mock_req.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value={"status": "completed", "clone_path": "ns/name"}
            ),
        )

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.create_clone_at_path(
                "/mnt/nfs/cidx/source", "/mnt/nfs/cidx/.versioned/ns/name"
            )

        assert "timeout" in mock_req.post.call_args.kwargs, (
            "requests.post() for clone creation must pass an explicit timeout "
            "kwarg -- a lost/dropped response otherwise hangs forever (Bug #1513)."
        )
        assert mock_req.post.call_args.kwargs["timeout"] is not None

    def test_poll_job_get_passes_timeout_kwarg(self):
        backend = self._make_mocked_backend()
        mock_req = MagicMock()
        mock_req.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value={"status": "completed", "clone_path": "ns/name"}
            ),
        )

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend._poll_job("job-1")

        assert "timeout" in mock_req.get.call_args.kwargs, (
            "requests.get() for job polling must pass an explicit timeout "
            "kwarg -- a lost/dropped response otherwise hangs forever (Bug #1513)."
        )
        assert mock_req.get.call_args.kwargs["timeout"] is not None

    def test_delete_clone_passes_timeout_kwarg(self):
        backend = self._make_mocked_backend()
        mock_req = MagicMock()
        mock_req.delete.return_value = MagicMock(status_code=204)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.delete_clone("/mnt/nfs/cidx/.versioned/ns/name")

        assert "timeout" in mock_req.delete.call_args.kwargs
        assert mock_req.delete.call_args.kwargs["timeout"] is not None

    def test_list_clones_passes_timeout_kwarg(self):
        backend = self._make_mocked_backend()
        mock_req = MagicMock()
        mock_req.get.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value=[])
        )

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.list_clones("ns")

        assert "timeout" in mock_req.get.call_args.kwargs
        assert mock_req.get.call_args.kwargs["timeout"] is not None

    def test_clone_exists_passes_timeout_kwarg(self):
        backend = self._make_mocked_backend()
        mock_req = MagicMock()
        mock_req.get.return_value = MagicMock(status_code=404)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.clone_exists("ns", "name")

        assert "timeout" in mock_req.get.call_args.kwargs
        assert mock_req.get.call_args.kwargs["timeout"] is not None
