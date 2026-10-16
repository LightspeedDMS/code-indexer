"""Repository URLs are masked in log output and error messages (CLI remote
client: api_clients/ and remote/).

Each path is driven with a repository URL whose userinfo carries a secret
placeholder. Local git repositories are real; the remote server is
replaced by fakes, and no path exercised here makes a network call.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from code_indexer.api_clients.repository_linking_client import (
    RepositoryLinkingClient,
    RepositoryNotFoundError,
)
from code_indexer.remote.query_execution import _establish_repository_link
from code_indexer.remote.repository_linking import ExactBranchMatcher
from code_indexer.remote.services.repository_service import RemoteRepositoryService

SECRET = "s3cr3t-value"
REPO_URL = f"https://example-user:{SECRET}@git.example.com/example/repo.git"


def _git_repo_with_origin(path: Path, origin: str) -> Path:
    for args in (
        ["init", "-q", "-b", "main"],
        ["remote", "add", "origin", origin],
        ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q"]
        + ["--allow-empty", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def _assert_logged_without_secret(caplog: pytest.LogCaptureFixture, text: str) -> None:
    messages = [r.getMessage() for r in caplog.records]
    assert any(text in m for m in messages), f"no log line containing {text!r}"
    assert all(SECRET not in m for m in messages)


def test_invalid_repository_url_error_omits_userinfo() -> None:
    client = RepositoryLinkingClient(
        server_url="http://127.0.0.1:9",
        credentials={"username": "example-user", "password": "example-pass"},
    )

    with pytest.raises(ValueError) as raised:
        client.discover_repositories(
            f"ftp://example-user:{SECRET}@git.example.com/example/repo"
        )

    assert "Invalid git URL format" in str(raised.value)
    assert SECRET not in str(raised.value)


def test_repository_linking_attempt_log_omits_userinfo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _git_repo_with_origin(tmp_path, REPO_URL)
    config_dir = repo / ".code-indexer"
    config_dir.mkdir()
    (config_dir / ".remote-config").write_text(
        '{"server_url": "http://127.0.0.1:9", '
        '"encrypted_credentials": "not-decryptable"}'
    )
    caplog.set_level(logging.DEBUG)

    # Remote mode is detected; linking logs its attempt, then stops at the
    # undecryptable credentials before any request is made.
    with pytest.raises(Exception) as raised:
        _establish_repository_link(repo)

    assert SECRET not in str(raised.value)
    _assert_logged_without_secret(caplog, "Attempting repository linking")


class _RepositoryNotFoundClient:
    """Repository client whose discovery finds nothing."""

    def discover_repositories(self, repo_url: str) -> Any:
        raise RepositoryNotFoundError("not found", 404)


def test_discovery_miss_log_omits_userinfo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _git_repo_with_origin(tmp_path, REPO_URL)
    caplog.set_level(logging.DEBUG)
    # The fake implements only discover_repositories, the one call made
    # before this path returns, so it is not a RepositoryLinkingClient.
    matcher = ExactBranchMatcher(cast(Any, _RepositoryNotFoundClient()))

    assert matcher.find_exact_branch_match(repo, REPO_URL) is None
    _assert_logged_without_secret(caplog, "No repositories found for URL")


class _UnreachableApiClient:
    """API client whose requests fail."""

    def get(self, path: str) -> Any:
        raise ConnectionError("server unreachable")


def test_repository_analysis_log_omits_userinfo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    # Fakes stand in for the server client and the staleness detector,
    # neither of which is reached with an empty repository list.
    service = RemoteRepositoryService(
        cast(Any, _UnreachableApiClient()), cast(Any, None)
    )

    service.get_repository_analysis(REPO_URL, "main")

    _assert_logged_without_secret(caplog, "Starting repository analysis")


def test_discovery_request_carries_no_url_credentials(tmp_path: Path) -> None:
    """The repository URL is sent to the server without its userinfo."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit

    requested: list = []

    class _Server(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # login
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._reply(200, {"access_token": "example-jwt", "token_type": "bearer"})

        def do_GET(self) -> None:
            requested.append(self.path)
            self._reply(404, {"detail": "Repository not found"})

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = RepositoryLinkingClient(
            server_url=f"http://127.0.0.1:{server.server_address[1]}",
            credentials={"username": "example-user", "password": "example-pass"},
            project_root=tmp_path,
        )
        with pytest.raises(RepositoryNotFoundError):
            client.discover_repositories(REPO_URL)
    finally:
        server.shutdown()
        server.server_close()

    discovery = [p for p in requested if p.startswith("/api/repos/discover")]
    assert len(discovery) == 1
    (source,) = parse_qs(urlsplit(discovery[0]).query)["source"]
    assert source == "https://git.example.com/example/repo.git"
    assert SECRET not in discovery[0]


class _EncodedUrlNotFoundClient:
    """Discovery fails with server error text quoting the repository URL."""

    def discover_repositories(self, repo_url: str) -> Any:
        from urllib.parse import quote

        raise RepositoryNotFoundError(
            f"Discovery failed: no repository for {repo_url} "
            f"(source={quote(repo_url, safe='')})",
            404,
        )


def test_discovery_miss_log_masks_exception_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _git_repo_with_origin(tmp_path, REPO_URL)
    caplog.set_level(logging.DEBUG)
    matcher = ExactBranchMatcher(cast(Any, _EncodedUrlNotFoundClient()))

    assert matcher.find_exact_branch_match(repo, REPO_URL) is None
    _assert_logged_without_secret(caplog, "No repositories found for URL")


def test_activation_prompt_omits_userinfo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from code_indexer.remote.repository_linking import (
        AutoRepositoryActivator,
        MatchQuality,
        RepositoryMatch,
        RepositoryType,
    )

    golden = RepositoryMatch(
        alias="example-repo",
        repository_type=list(RepositoryType)[0],
        branch="main",
        match_quality=list(MatchQuality)[0],
        priority=1,
        git_url=REPO_URL,
        display_name="Example",
        description="",
        available_branches=["main"],
        last_updated="",
        access_level="read",
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    activator = AutoRepositoryActivator(cast(Any, None))

    assert activator._confirm_activation(golden, "example-alias") is False

    output = capsys.readouterr().out
    assert "git.example.com/example/repo.git" in output
    assert SECRET not in output
