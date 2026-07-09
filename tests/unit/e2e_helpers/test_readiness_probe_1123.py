"""Unit tests for the bash readiness probe (Story #1123 AC2).

AC2 requires that the bash wait_for_server function in e2e-automation.sh
verifies BOTH:
  1. /health returns non-5xx
  2. POST /auth/login returns 200 with a token

This test file sources e2e-automation.sh directly (making e2e-automation.sh
source-safe is AC2 prerequisite) and drives the REAL bash wait_for_server
function against stub HTTP servers.

Single source of truth: the bash predicate is now tested directly, eliminating
the phantom-coverage class where a Python twin was tested while the real bash
gate had only string-grep coverage.

Mutation check design:
  - A real stub HTTP server is started (using http.server in a thread) on a
    free local port.
  - A bash subprocess sources e2e-automation.sh and calls wait_for_server,
    passing E2E_SERVER_HOST/PORT/ADMIN_USER/PASS and a short readiness timeout.
  - always-503 stub => wait_for_server exits non-zero (readiness FAILS)
  - health-200-but-/auth/login-fails stub => readiness FAILS
  - healthy stub (200 /health AND /auth/login 200+token) => readiness PASSES

All checks exercise real bash + real network I/O; nothing is mocked.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
E2E_SCRIPT = REPO_ROOT / "e2e-automation.sh"

# Short probe timeouts so failing stubs return quickly in CI
_READINESS_TIMEOUT = 3
_READINESS_POLL = 1


# ---------------------------------------------------------------------------
# Stub HTTP server infrastructure
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return a free TCP port by binding and immediately releasing."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_stub_server(port: int, handler_class: type) -> HTTPServer:
    """Create an HTTPServer with SO_REUSEADDR on the given port."""
    import socket

    server = HTTPServer(("127.0.0.1", port), handler_class)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return server


def _start_stub(server: HTTPServer) -> threading.Thread:
    """Start an HTTPServer in a daemon thread; returns the thread."""
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Handler: always returns 503
# ---------------------------------------------------------------------------


class _Always503Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.end_headers()

    def do_POST(self):
        self.send_response(503)
        self.end_headers()

    def log_message(self, *args):
        pass  # suppress request logs in test output


# ---------------------------------------------------------------------------
# Handler: /health returns 200, /auth/login returns 503
# ---------------------------------------------------------------------------


class _HealthOkAuthFailHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # /auth/login always fails
        self.send_response(503)
        self.end_headers()

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------------------
# Handler: /health OK, /auth/login returns 200 with access_token
# ---------------------------------------------------------------------------


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/auth/login":
            # Drain request body
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length:
                self.rfile.read(content_length)
            body = json.dumps({"access_token": "test-jwt-token"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------------------
# Helper: run bash wait_for_server by sourcing e2e-automation.sh
# ---------------------------------------------------------------------------


def _run_bash_wait_for_server(
    port: int, timeout: int = _READINESS_TIMEOUT
) -> subprocess.CompletedProcess:
    """Source e2e-automation.sh and call wait_for_server against the given port.

    Returns a CompletedProcess; callers check returncode.
    The script is sourced (not executed) so only function definitions are
    loaded — the main body credential-exit and phase-loop do NOT run.
    """
    script = str(E2E_SCRIPT)
    bash_cmd = (
        f"source {script!r}; "
        f"E2E_SERVER_HOST=127.0.0.1 "
        f"E2E_SERVER_PORT={port} "
        f"E2E_ADMIN_USER=testuser "
        f"E2E_ADMIN_PASS=testpass "
        f"E2E_SERVER_READINESS_TIMEOUT={timeout} "
        f"E2E_SERVER_READINESS_POLL=0.5 "
        f"wait_for_server"
    )
    return subprocess.run(
        ["bash", "-c", bash_cmd],
        capture_output=True,
        text=True,
        timeout=timeout + 10,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBashWaitForServerMutation:
    """Mutation tests that drive the REAL bash wait_for_server function.

    These tests source e2e-automation.sh directly so any change to the bash
    gate is immediately reflected here -- no drift between the test and the
    production gate is possible.
    """

    def test_script_is_sourceable(self):
        """e2e-automation.sh must be source-safe (sourcing must not execute main body).

        If the script exits on credential checks when sourced this test will
        fail with a non-zero exit code from the source-only invocation.
        """
        result = subprocess.run(
            ["bash", "-c", f"source {str(E2E_SCRIPT)!r}; echo source_ok"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Sourcing e2e-automation.sh must not execute the main body "
            f"(credential exits or phase-loop). Got rc={result.returncode}, "
            f"stderr={result.stderr!r}"
        )
        assert "source_ok" in result.stdout, (
            f"Expected 'source_ok' in stdout after sourcing; got: {result.stdout!r}"
        )

    def test_wait_for_server_defined_after_source(self):
        """wait_for_server must be available as a bash function after sourcing."""
        result = subprocess.run(
            ["bash", "-c", f"source {str(E2E_SCRIPT)!r}; declare -f wait_for_server"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"wait_for_server must be defined after sourcing. "
            f"rc={result.returncode}, stderr={result.stderr!r}"
        )
        assert "wait_for_server" in result.stdout

    def test_rejects_always_503_server(self):
        """A server that always returns 503 MUST fail readiness (bash gate).

        This is the primary mutation: even a bound process cannot fool the
        hardened bash probe if it cannot serve HTTP traffic correctly.
        """
        port = _find_free_port()
        server = _make_stub_server(port, _Always503Handler)
        _start_stub(server)
        try:
            result = _run_bash_wait_for_server(port, timeout=_READINESS_TIMEOUT)
            assert result.returncode != 0, (
                f"wait_for_server must FAIL on a 503-only server "
                f"(rc={result.returncode}, stdout={result.stdout!r})"
            )
        finally:
            server.shutdown()

    def test_rejects_health_ok_but_auth_fail(self):
        """A server returning 200 on /health but 503 on /auth/login MUST fail.

        This is the key hardening: an old health-only probe would accept this
        degraded server. The bash wait_for_server must reject it.
        """
        port = _find_free_port()
        server = _make_stub_server(port, _HealthOkAuthFailHandler)
        _start_stub(server)
        try:
            result = _run_bash_wait_for_server(port, timeout=_READINESS_TIMEOUT)
            assert result.returncode != 0, (
                f"wait_for_server must FAIL when /auth/login returns 503 "
                f"(rc={result.returncode}, stdout={result.stdout!r})"
            )
        finally:
            server.shutdown()

    def test_accepts_healthy_server(self):
        """A server returning 200 on /health AND 200+token on /auth/login PASSES."""
        port = _find_free_port()
        server = _make_stub_server(port, _HealthyHandler)
        _start_stub(server)
        try:
            result = _run_bash_wait_for_server(port, timeout=10)
            assert result.returncode == 0, (
                f"wait_for_server must PASS on a fully healthy server "
                f"(rc={result.returncode}, stdout={result.stdout!r}, "
                f"stderr={result.stderr!r})"
            )
        finally:
            server.shutdown()

    def test_fails_when_nothing_listening(self):
        """wait_for_server times out (exit non-zero) when no server is bound."""
        port = _find_free_port()
        # Nothing is started on this port -- connection refused on every poll
        result = _run_bash_wait_for_server(port, timeout=_READINESS_TIMEOUT)
        assert result.returncode != 0, (
            f"wait_for_server must time out when nothing listens on port {port} "
            f"(rc={result.returncode})"
        )
