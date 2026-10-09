"""Server-spawned watch mode uses server-managed provider settings, never a
repository-authored embedding-provider endpoint.

Every MCP file write triggers `auto_watch_manager.start_watch(repo_path)`
(`server/mcp/handlers/files.py`). `AutoWatchManager.start_watch()` loads the
repo's `.code-indexer/config.json` and hands the resulting `Config` straight
to `DaemonWatchManager.start_watch(config=config)`, which builds
`EmbeddingProviderFactory.create(config=config)` for the watch handler's
incremental-reindex-on-file-change path
(`code_indexer/daemon/watch_manager.py::_create_watch_handler`). A
repository-authored `voyage_ai.api_endpoint` must not be able to choose
where that client sends its request when a watched file changes.

This test drives the REAL `AutoWatchManager.start_watch()` end to end --
`DaemonWatchManager` is NOT mocked, so a real background watch thread, a
real `GitAwareWatchHandler`, and a real `VoyageAIClient` run. Only the HTTP
boundary itself (`httpx.Client.post`) is mocked, so neither test makes a
real outbound request to the real VoyageAI endpoint; the recorded call log
proves both that the watch-triggered reindex actually reached the
server-managed default endpoint (so a broken mock cannot silently pass) and
that the repository-configured endpoint never received anything. A real
local loopback listener is kept running as an independent, defense-in-depth
check that it is never really contacted. An autouse network guard proves
neither test needs real external network access at all. The poll loop
below exits as soon as the default endpoint's call is recorded, so the test
finishes in a few seconds rather than waiting out a fixed deadline.
"""

from __future__ import annotations

import http.server
import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import ConfigManager, VoyageAIConfig
from code_indexer.server.services.auto_watch_manager import AutoWatchManager
import code_indexer.server.utils.server_managed_provider_settings as smps


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    received: List[Optional[str]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        _RecordingHandler.received.append(self.headers.get("Authorization"))
        body = json.dumps({"data": [{"embedding": [0.0] * 1024}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
        pass


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    """Defense-in-depth: proves every test in this file needs only
    loopback connections (the in-test HTTP listener), never real external
    DNS/network access -- even if a test's own HTTP-boundary mock were
    somehow bypassed."""
    real_getaddrinfo = socket.getaddrinfo

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost", "::1", None):
            raise RuntimeError(
                f"blocked outbound DNS/connect to non-loopback host {host!r} -- "
                "this test must never reach real external network"
            )
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


@pytest.fixture
def loopback_listener():
    """A real local HTTP server standing in for a repository-configured
    endpoint a repo's config.json might name."""
    _RecordingHandler.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/embeddings"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _write_watched_repo(repo_path: Path, endpoint: str) -> None:
    """A real git repo with a real on-disk config.json naming `endpoint`."""
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "hello.py").write_text("def hello():\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "user.name", "T"], check=True
    )
    subprocess.run(["git", "-C", str(repo_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-q", "-m", "init"], check=True
    )

    config_manager = ConfigManager(repo_path / ".code-indexer" / "config.json")
    config_manager.create_default_config(codebase_dir=repo_path)
    config = config_manager.load()
    config.voyage_ai.api_endpoint = endpoint
    config_manager.save(config)


def _canned_post(called_urls: List[str], lock: threading.Lock):
    """A httpx.Client.post stand-in that records every called URL (under a
    lock -- the watch handler's embedding calls run on a background
    thread) and returns a valid embeddings-shaped response, no real
    network I/O."""

    def _fake_post(_self, url, *args, **kwargs):
        url_str = str(url)
        with lock:
            called_urls.append(url_str)
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.0] * 1024}]},
            request=httpx.Request("POST", url_str),
        )

    return _fake_post


def _run_watch_and_edit(
    repo_path: Path, called_urls: List[str], lock: threading.Lock, stop_predicate
) -> None:
    """Start the real watch, trigger a file change, and poll (bounded,
    exiting early via `stop_predicate`) before always stopping the watch."""
    manager = AutoWatchManager()
    result = manager.start_watch(str(repo_path), timeout=60)
    assert result["status"] == "success", result

    try:
        # Give the background watch thread time to construct its handler
        # before the file change that should trigger a real incremental
        # reindex (GitAwareWatchHandler debounces ~2s by default, plus
        # watchdog OS-level filesystem event latency).
        time.sleep(0.3)
        (repo_path / "hello.py").write_text("def hello():\n    return 2\n")

        deadline = time.time() + 13
        while time.time() < deadline:
            with lock:
                if stop_predicate(called_urls):
                    break
            time.sleep(0.2)
    finally:
        manager.stop_watch(str(repo_path))


# A real watch plus the debounced reindex, polled up to 13 s (~8.4 s idle):
# the wait is the behaviour under test, slower under gate load.
@pytest.mark.timeout(45)
def test_auto_watch_never_sends_repo_configured_provider_endpoint(
    tmp_path, loopback_listener, monkeypatch
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-test-key")
    repo_path = tmp_path / "repo"
    _write_watched_repo(repo_path, loopback_listener)

    default_endpoint = VoyageAIConfig().api_endpoint
    called_urls: List[str] = []
    lock = threading.Lock()

    with patch("httpx.Client.post", _canned_post(called_urls, lock)):
        _run_watch_and_edit(
            repo_path, called_urls, lock, lambda urls: default_endpoint in urls
        )

    assert default_endpoint in called_urls, (
        "the watch-triggered reindex never reached the server-managed "
        f"default endpoint -- got {called_urls}"
    )
    assert loopback_listener not in called_urls, (
        "SECURITY: the watch-triggered reindex targeted the repository-"
        f"configured embedding endpoint: {called_urls}"
    )
    assert _RecordingHandler.received == [], (
        "SECURITY: the real loopback listener received a request: "
        f"{_RecordingHandler.received}"
    )


# Same real watch and debounced-reindex poll (~7.5 s idle) as above.
@pytest.mark.timeout(45)
def test_auto_watch_targets_repo_endpoint_when_enforcement_is_skipped(
    tmp_path, loopback_listener, monkeypatch
) -> None:
    """Teeth check: with `enforce_server_managed_provider_settings` replaced
    by a no-op, the watch-triggered reindex targets the repository-
    configured endpoint instead of the server-managed default -- proving
    the main test above is discriminating, not vacuously green. Patches
    the function at its OWN defining module (never the real
    auto_watch_manager.py source), which the local `from
    ..utils.server_managed_provider_settings import
    enforce_server_managed_provider_settings` inside
    `AutoWatchManager.start_watch()` re-resolves on every call."""
    monkeypatch.setattr(
        smps, "enforce_server_managed_provider_settings", lambda config: None
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-test-key")
    repo_path = tmp_path / "repo"
    _write_watched_repo(repo_path, loopback_listener)

    called_urls: List[str] = []
    lock = threading.Lock()

    with patch("httpx.Client.post", _canned_post(called_urls, lock)):
        _run_watch_and_edit(repo_path, called_urls, lock, lambda urls: bool(urls))

    assert loopback_listener in called_urls, (
        "teeth check failed: without enforcement, the watch-triggered "
        f"reindex should have targeted the repository-configured endpoint. "
        f"Got {called_urls}"
    )
