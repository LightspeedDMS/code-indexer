"""REST hard reset, clean and branch delete: confirmation tokens through
the real routes, with the shared store wired by the real server startup.

Invariants:
  - server startup wires the cluster-shared PayloadCache into the git
    service, so tokens are never kept in one process's memory; when that
    startup step fails, the service is left unwired and fails loudly;
  - an invalid confirmation token returns 200 with the documented
    ``requires_confirmation`` + ``token`` shape (never a 4xx/5xx) plus a
    ``message``, the operation does not run, and the fresh token then
    confirms it;
  - branch delete takes the token in the X-Confirmation-Token header, the
    in-repo client sends it there, and a token that still arrives in a URL
    query is never written to a log in clear.

Real git repositories; only the alias-to-path lookup and the
authenticated-user dependency are test doubles.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.api_clients.git_client import GitAPIClient
from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)
from tests.unit.server.services._git_confirm_helpers import (
    BRANCH,
    COMMITTED_TEXT,
    TRACKED,
    UNTRACKED,
    AliasPaths,
    git,
    make_repo,
)

_ALIAS = "myrepo"
_BASE = f"/api/v1/repos/{_ALIAS}/git"
_BOGUS_TOKEN = "ZZZZZZ"
_TOKEN_HEADER = "X-Confirmation-Token"
_QUERY_TOKEN_VALUE = "QueryTokenValueExample"
_HEADER_TOKEN_VALUE = "HeaderTokenValueExample"


class _Capture(logging.Handler):
    """Collects formatted log lines."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@contextmanager
def _running_client(
    tmp_path: Path, *startup_patches: Any
) -> Iterator[Tuple[TestClient, Path]]:
    """A TestClient over the real app; `startup_patches` are active while
    the real lifespan runs."""
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _access_service_admin,
    )

    repo = make_repo(tmp_path / "repos", _ALIAS)
    user = Mock()
    user.username = "rest-admin"
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with ExitStack() as stack:
            # payload_cache starts unset: only server startup may wire it.
            stack.enter_context(
                patch.object(git_operations_service, "payload_cache", None)
            )
            # The backing slot, not the property: reading the property
            # would construct the real ActivatedRepoManager.
            stack.enter_context(
                patch.object(
                    git_operations_service,
                    "_activated_repo_manager_lazy",
                    AliasPaths({_ALIAS: repo}),
                )
            )
            for startup_patch in startup_patches:
                stack.enter_context(startup_patch)
            # A process-wide chunk-store cache left by an earlier test
            # without a lease root makes the real PayloadCache startup
            # step fail; start from a clean one.
            from code_indexer.storage.shared.chunk_store_cache import (
                reset_global_chunk_store_cache,
            )

            reset_global_chunk_store_cache()
            client = stack.enter_context(TestClient(app))
            stack.enter_context(
                _access_service_admin(tmp_path / "access-groups.db", user.username)
            )
            yield client, repo
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client_and_repo(tmp_path: Path) -> Iterator[Tuple[TestClient, Path]]:
    with _running_client(tmp_path) as running:
        yield running


def _reset(client: TestClient, token: Optional[str]) -> Any:
    body: Dict[str, Any] = {"mode": "hard", "commit_hash": "HEAD"}
    if token is not None:
        body["confirmation_token"] = token
    return client.post(f"{_BASE}/reset", json=body)


def _clean(client: TestClient, token: Optional[str]) -> Any:
    body: Dict[str, Any] = {}
    if token is not None:
        body["confirmation_token"] = token
    return client.post(f"{_BASE}/clean", json=body)


def _delete(client: TestClient, token: Optional[str]) -> Any:
    params = {} if token is None else {"confirmation_token": token}
    return client.delete(f"{_BASE}/branches/{BRANCH}", params=params)


def _branch_gone(repo: Path) -> bool:
    return git(["branch", "--list", BRANCH], repo).strip() == ""


_ROUTES: Dict[
    str, Tuple[Callable[[TestClient, Optional[str]], Any], Callable[[Path], bool]]
] = {
    "reset_hard": (_reset, lambda r: (r / TRACKED).read_text() == COMMITTED_TEXT),
    "clean": (_clean, lambda r: not (r / UNTRACKED).exists()),
    "branch_delete": (_delete, _branch_gone),
}


def test_server_startup_wires_the_shared_store(
    client_and_repo: Tuple[TestClient, Path],
) -> None:
    assert git_operations_service.payload_cache is not None
    assert git_operations_service.payload_cache is app.state.payload_cache


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_invalid_token_returns_fresh_token_that_confirms(
    client_and_repo: Tuple[TestClient, Path], route: str
) -> None:
    client, repo = client_and_repo
    call, happened = _ROUTES[route]

    rejected = call(client, _BOGUS_TOKEN)

    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body.get("requires_confirmation") is True, body
    fresh = body.get("token")
    assert isinstance(fresh, str) and fresh and fresh != _BOGUS_TOKEN
    assert body.get("success") is not True
    assert not happened(repo)

    confirmed = call(client, fresh)

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json().get("success") is True, confirmed.text
    assert happened(repo)


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_rejection_carries_a_message_and_first_call_does_not(
    client_and_repo: Tuple[TestClient, Path], route: str
) -> None:
    client, _ = client_and_repo
    call, _ = _ROUTES[route]

    first = call(client, None).json()
    rejected = call(client, _BOGUS_TOKEN).json()

    assert first.get("message") is None, first
    assert "invalid or expired" in str(rejected.get("message")).lower(), rejected


def test_branch_delete_accepts_the_token_in_a_header(
    client_and_repo: Tuple[TestClient, Path],
) -> None:
    client, repo = client_and_repo
    token = _delete(client, None).json()["token"]

    confirmed = client.delete(
        f"{_BASE}/branches/{BRANCH}", headers={_TOKEN_HEADER: token}
    )

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json().get("success") is True, confirmed.text
    assert _branch_gone(repo)


def test_branch_delete_handles_a_slashed_branch_name(
    client_and_repo: Tuple[TestClient, Path],
) -> None:
    client, repo = client_and_repo
    git(["branch", "topic/x"], repo)
    url = f"{_BASE}/branches/topic%2Fx"

    first = client.delete(url)
    assert first.status_code == 200, first.text
    token = first.json()["token"]
    confirmed = client.delete(url, headers={_TOKEN_HEADER: token})

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json().get("success") is True, confirmed.text
    assert git(["branch", "--list", "topic/x"], repo).strip() == ""


def test_server_startup_installs_access_log_redaction(
    client_and_repo: Tuple[TestClient, Path],
) -> None:
    access_logger = logging.getLogger("uvicorn.access")
    capture = _Capture()
    previous_level = access_logger.level
    access_logger.addHandler(capture)
    access_logger.setLevel(logging.INFO)
    path = f"{_BASE}/branches/{BRANCH}?confirmation_token={_QUERY_TOKEN_VALUE}"
    try:
        # The exact call shape uvicorn's HTTP protocols use for access lines.
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d', "192.0.2.10:50000", "DELETE", path, "1.1", 200
        )
    finally:
        access_logger.removeHandler(capture)
        access_logger.setLevel(previous_level)

    assert len(capture.lines) == 1
    assert _QUERY_TOKEN_VALUE not in capture.lines[0]
    assert f"{_BASE}/branches/{BRANCH}" in capture.lines[0]


def test_error_log_never_records_a_confirmation_token(
    tmp_path: Path, caplog: Any
) -> None:
    # An unwired store makes branch delete fail with 500, so the error
    # handler logs the failed request, query string included.
    failing_step = patch(
        "code_indexer.storage.shared.chunk_store_cache_cross_process"
        ".register_payload_cache",
        side_effect=RuntimeError("startup step failed"),
    )
    caplog.set_level(logging.INFO)
    with _running_client(tmp_path, failing_step) as (client, _):
        response = client.delete(
            f"{_BASE}/branches/{BRANCH}",
            params={"confirmation_token": _QUERY_TOKEN_VALUE},
            headers={_TOKEN_HEADER: _HEADER_TOKEN_VALUE},
        )

    assert response.status_code == 500, response.text
    # The test's own HTTP client logs its request URL under "httpx"; only
    # the server's records are under test here.
    server_lines = [r.getMessage() for r in caplog.records if r.name != "httpx"]
    assert any(
        f"{_BASE}/branches/{BRANCH}" in line and "Query" in line
        for line in server_lines
    ), "the failed request and its query must be logged"
    assert not any(_QUERY_TOKEN_VALUE in line for line in server_lines)
    assert not any(_HEADER_TOKEN_VALUE in line for line in server_lines)


def test_cli_client_branch_delete_sends_the_token_in_a_header(
    client_and_repo: Tuple[TestClient, Path],
) -> None:
    client, repo = client_and_repo
    sent: List[Tuple[str, str, Dict[str, Any]]] = []

    def _through_app(method: str, endpoint: str, **kwargs: Any) -> Any:
        sent.append((method, endpoint, kwargs))
        return client.request(method, endpoint, **kwargs)

    git_client = GitAPIClient(
        server_url="https://example.com",
        credentials={"username": "example-user", "password": "example-pass"},
    )
    with patch.object(git_client, "_authenticated_request", side_effect=_through_app):
        token = git_client.branch_delete(_ALIAS, BRANCH)["token"]
        result = git_client.branch_delete(_ALIAS, BRANCH, confirmation_token=token)

    assert result.get("success") is True, result
    assert _branch_gone(repo)
    for _, endpoint, kwargs in sent:
        assert token not in endpoint
        assert token not in str(kwargs.get("params", ""))
    assert sent[-1][2]["headers"][_TOKEN_HEADER] == token


def test_failed_cache_startup_leaves_git_service_unwired(tmp_path: Path) -> None:
    failing_step = patch(
        "code_indexer.storage.shared.chunk_store_cache_cross_process"
        ".register_payload_cache",
        side_effect=RuntimeError("startup step failed"),
    )
    with _running_client(tmp_path, failing_step) as (client, repo):
        assert app.state.payload_cache is None
        assert git_operations_service.payload_cache is None

        response = _delete(client, None)

    assert response.status_code == 500, response.text
    assert not _branch_gone(repo)
