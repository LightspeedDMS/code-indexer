"""Server queries use server-managed provider endpoints; repository
configuration cannot select them.

`search_service._load_repo_config()` is the seam that loads a repo's
`.code-indexer/config.json` for every server-side semantic query (single-repo
search_repository_path / search_repository_path_with_provider,
query_multimodal_only, and reused by multi_search_service.py for its
per-repo semantic fan-out). The returned `Config` feeds
`EmbeddingProviderFactory.create(config, ...)` directly, so a
repository-authored `voyage_ai.api_endpoint` / `cohere.api_endpoint` would
otherwise choose where the resulting client sends its request -- carrying
whatever API key the server process has configured.

These tests drive the REAL `_load_repo_config()`, `EmbeddingProviderFactory`,
and `VoyageAIClient` against a real local loopback HTTP listener (no mocks on
the code under test); only the HTTP boundary itself (`httpx.Client.post`) is
ever mocked, in BOTH tests -- neither test may make a real outbound request
to the real VoyageAI endpoint. The recorded call log proves both that the
query actually reached the server-managed default endpoint (so a broken
mock cannot silently pass) and that the repository-configured endpoint never
received anything. An autouse network guard proves neither test needs real
external network access at all.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Callable, List, Optional
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.server.services import search_service as ss
from code_indexer.services.embedding_factory import EmbeddingProviderFactory
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
    endpoint a repository's config.json might name."""
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


@pytest.fixture
def repo_with_endpoint(tmp_path: Path, monkeypatch) -> Callable[[str], Path]:
    """Force the direct-load branch of _load_repo_config (no RepoConfigCache
    wired), matching CLI-in-process / unit-test conditions, and return a
    factory that plants a repo config.json naming a given endpoint."""
    monkeypatch.setattr(ss, "_get_repo_config_cache", lambda: None)

    def _make(endpoint: str) -> Path:
        repo_path = tmp_path / "repo"
        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "codebase_dir": str(repo_path),
                    "embedding_provider": "voyage-ai",
                    "voyage_ai": {"api_endpoint": endpoint},
                }
            )
        )
        return repo_path

    return _make


def _canned_post(called_urls: List[str]):
    """A httpx.Client.post stand-in that records every called URL and
    returns a valid embeddings-shaped response -- no real network I/O."""

    def _fake_post(_self, url, *args, **kwargs):
        url_str = str(url)
        called_urls.append(url_str)
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.0] * 1024}]},
            request=httpx.Request("POST", url_str),
        )

    return _fake_post


def test_query_path_config_load_never_lets_repo_choose_provider_endpoint(
    repo_with_endpoint, loopback_listener, monkeypatch
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key-should-never-reach-listener")
    repo_path = repo_with_endpoint(loopback_listener)

    config = ss._load_repo_config(str(repo_path))
    provider = EmbeddingProviderFactory.create(config, console=None)

    default_endpoint = VoyageAIConfig().api_endpoint
    called_urls: List[str] = []

    with patch("httpx.Client.post", _canned_post(called_urls)):
        embedding = provider.get_embedding("hello world")

    assert len(embedding) == 1024, embedding
    assert default_endpoint in called_urls, (
        "the query never reached the server-managed default endpoint -- "
        f"got {called_urls}"
    )
    assert loopback_listener not in called_urls, (
        "SECURITY: the query-path repo-config loader let a repository-"
        f"chosen embedding endpoint be targeted: {called_urls}"
    )
    assert _RecordingHandler.received == [], (
        "SECURITY: the real loopback listener received a request: "
        f"{_RecordingHandler.received}"
    )


def test_query_path_still_reaches_the_server_managed_endpoint(
    repo_with_endpoint, loopback_listener, monkeypatch
) -> None:
    """Positive control: after the reset, the client still functions --
    proven by mocking ONLY the HTTP boundary (httpx.Client.post) and
    asserting it is called with the fixed server-managed default endpoint,
    never the repository-chosen one."""
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    repo_path = repo_with_endpoint(loopback_listener)

    config = ss._load_repo_config(str(repo_path))
    provider = EmbeddingProviderFactory.create(config, console=None)

    default_endpoint = VoyageAIConfig().api_endpoint
    called_urls: List[str] = []

    with patch("httpx.Client.post", _canned_post(called_urls)):
        embedding = provider.get_embedding("hello world")

    assert len(embedding) == 1024
    assert called_urls == [default_endpoint], called_urls
    assert _RecordingHandler.received == []


def test_query_path_targets_repo_endpoint_when_enforcement_is_skipped(
    repo_with_endpoint, loopback_listener, monkeypatch
) -> None:
    """Teeth check: with `enforce_server_managed_provider_settings` replaced
    by a no-op, `_load_repo_config()` returns the repository-configured
    endpoint unmodified -- proving the main test above is discriminating,
    not vacuously green. Patches the function at its OWN defining module
    (never the real search_service.py source), which the local `from
    ..utils.server_managed_provider_settings import
    enforce_server_managed_provider_settings` inside `_load_repo_config()`
    re-resolves on every call."""
    monkeypatch.setattr(
        smps, "enforce_server_managed_provider_settings", lambda config: None
    )
    repo_path = repo_with_endpoint(loopback_listener)

    config = ss._load_repo_config(str(repo_path))

    assert config.voyage_ai.api_endpoint == loopback_listener, (
        "teeth check failed: without enforcement, _load_repo_config() "
        f"should have returned the repository-configured endpoint. Got "
        f"{config.voyage_ai.api_endpoint!r}"
    )
