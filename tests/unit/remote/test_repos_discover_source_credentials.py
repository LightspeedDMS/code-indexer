"""Repository discovery never sends or echoes repository URL credentials.

The client is driven against a loopback HTTP server; the discovery service
runs with a repository matcher that finds nothing. No network call leaves
the machine. An ssh login user is kept (it is needed and not secret); every
password is removed.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from code_indexer.server.git.git_subprocess_env import remote_url_without_credentials

SECRET = "s3cr3t-value"
# (url with a password, its credential-free form)
FORMS = [
    (
        f"https://example-user:{SECRET}@git.example.com/example/repo.git",
        "https://git.example.com/example/repo.git",
    ),
    (
        f"ssh://git:{SECRET}@git.example.com/example/repo.git",
        "ssh://git@git.example.com/example/repo.git",
    ),
    (
        f"git+ssh://git:{SECRET}@git.example.com:2222/example/repo.git",
        "git+ssh://git@git.example.com:2222/example/repo.git",
    ),
]


HELPER_TABLE = list(FORMS)
HELPER_TABLE.extend(
    [
        # Surrounding whitespace.
        (f" ssh://git:{SECRET}@forge.example/o/r ", "ssh://git@forge.example/o/r"),
        (f" https://{SECRET}@forge.example/o/r ", "https://forge.example/o/r"),
        # Userinfo runs to the authority's LAST '@'.
        (f"ssh://git@x:{SECRET}@forge.example/o/r", "ssh://forge.example/o/r"),
        (f"ssh://git:{SECRET}:x@y@forge.example/o/r", "ssh://git@forge.example/o/r"),
        # Percent-encoded userinfo.
        # The login is the raw text before the first literal ':' and is kept
        # only if it decodes to a plain name; otherwise no userinfo is kept.
        (f"ssh://git%3A{SECRET}@forge.example/o/r", "ssh://forge.example/o/r"),
        (f"ssh://{SECRET}%3Afoo@forge.example/o/r", "ssh://forge.example/o/r"),
        (f"ssh://u%3Av:{SECRET}@forge.example/o/r", "ssh://forge.example/o/r"),
        (f"ssh://u%40v:{SECRET}@forge.example/o/r", "ssh://forge.example/o/r"),
        (f"https://u%40v:{SECRET}%3A@forge.example/o/r", "https://forge.example/o/r"),
        # Every ssh-family scheme, any case.
        (
            f"ssh+git://git:{SECRET}@forge.example/o/r",
            "ssh+git://git@forge.example/o/r",
        ),
        (
            f"GIT+SSH://git:{SECRET}@forge.example:2222/o/r",
            "GIT+SSH://git@forge.example:2222/o/r",
        ),
        # Unchanged for every caller's existing form.
        (
            "ssh://git@git.example.com/example/repo.git",
            "ssh://git@git.example.com/example/repo.git",
        ),
        (
            "git@git.example.com:example/repo.git",
            "git@git.example.com:example/repo.git",
        ),
        ("https://forge.example/o/r.git?x=1#f", "https://forge.example/o/r.git?x=1#f"),
        ("/srv/golden/repo", "/srv/golden/repo"),
    ]
)


@pytest.mark.parametrize("url,expected", HELPER_TABLE)
def test_remote_url_without_credentials_drops_every_password(
    url: str, expected: str
) -> None:
    result = remote_url_without_credentials(url)

    assert result == expected
    assert SECRET not in result


@pytest.mark.parametrize("url,expected", FORMS)
def test_discover_request_carries_no_url_credentials(
    tmp_path: Path, url: str, expected: str
) -> None:
    from code_indexer.api_clients.base_client import APIClientError
    from code_indexer.api_clients.repos_client import ReposAPIClient

    requested: List[str] = []

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
            self._reply(404, {"detail": "not found"})

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ReposAPIClient(
            server_url=f"http://127.0.0.1:{server.server_address[1]}",
            credentials={"username": "example-user", "password": "example-pass"},
            project_root=tmp_path,
        )
        with pytest.raises(APIClientError):
            result = client.discover_repositories(url)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
    finally:
        server.shutdown()
        server.server_close()

    discover = [p for p in requested if p.startswith("/api/repos/discover")]
    assert len(discover) == 1
    assert parse_qs(urlsplit(discover[0]).query)["source"] == [expected]
    assert SECRET not in discover[0]


class _NoMatches:
    def find_all_matching_repositories(self, canonical_url: str, user: Any):
        return [], []


class _User:
    username = "example-user"


@pytest.mark.parametrize("url,expected", FORMS)
def test_discovery_response_echoes_the_credential_free_url(
    url: str, expected: str
) -> None:
    from code_indexer.server.services.repository_discovery_service import (
        RepositoryDiscoveryService,
    )

    matcher = cast(Any, _NoMatches())  # fake: not a RepositoryMatcher
    user = cast(Any, _User())  # fake: not a User
    service = RepositoryDiscoveryService(
        golden_repo_manager=None,
        activated_repo_manager=None,
        repository_matcher=matcher,
    )

    response = service.discover_repositories(url, user)

    assert response.query_url == expected
    assert SECRET not in response.model_dump_json()
