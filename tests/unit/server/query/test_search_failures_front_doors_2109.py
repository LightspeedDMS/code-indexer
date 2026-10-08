"""Search failures are reported at the server front doors, never answered
with an empty result.

REST ``/api/query``: a store failure answers 5xx, a search timeout 504 and a
missing provider key an explicit 5xx error; a client validation error stays
4xx. MCP ``search_code``: the same failures answer ``success: false`` with the
error message.

Real route / handler, real SemanticQueryManager, SemanticSearchService and
FilesystemVectorStore over a real git repo. Replaced: the embedding provider
(external service), authentication, the activated-repo listing, and -- per
test -- the store search call, made to fail the way storage or a deadline
does.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.services.multi_index_query_service import (
    MultiIndexQueryTimeoutError,
)
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.extension_filter_env_2047 import (
    QUERY,
    REPO_ALIAS,
    build_corpus_repo,
    ranked_store_search,
)
from tests.unit.server.query.test_file_extensions_front_doors_2047 import ADMIN

STORE_SEARCH = (
    "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore.search"
)
PROVIDER_FACTORY = (
    "code_indexer.server.services.search_service.EmbeddingProviderFactory.create"
)
STORE_FAILURE = OSError("index storage is unavailable")
MISSING_KEY = ValueError("VOYAGE_API_KEY environment variable is required")


def _timeout() -> MultiIndexQueryTimeoutError:
    return MultiIndexQueryTimeoutError(["code"], 5.0)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tuple[Any, Path]]:
    root = tmp_path_factory.mktemp("search-failures-2109")
    repo = build_corpus_repo(root)
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("CO_API_KEY", raising=False)
        mp.delenv("VOYAGE_API_KEY", raising=False)
        with isolated_app(root / "app") as app:
            yield app, repo


def _user_repos(repo: Path) -> list:
    return [{"user_alias": REPO_ALIAS, "repo_path": str(repo)}]


@pytest.fixture
def rest(env: Tuple[Any, Path]) -> Iterator[TestClient]:
    from code_indexer.server.auth.dependencies import get_current_user

    app, repo = env
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    arm = app.state.semantic_query_manager.activated_repo_manager
    try:
        with (
            ranked_store_search(),
            patch.object(
                arm, "list_activated_repositories", return_value=_user_repos(repo)
            ),
            patch.object(arm, "get_activated_repo_path", return_value=str(repo)),
        ):
            yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _post(client: TestClient, **extra: Any) -> Any:
    body: Dict[str, Any] = {
        "query_text": QUERY,
        "repository_alias": REPO_ALIAS,
        "limit": 5,
        "search_mode": "semantic",
    }
    body.update(extra)
    return client.post("/api/query", json=body)


# --------------------------------------------------------------------- REST


def test_rest_healthy_search_answers_200_with_results(rest) -> None:
    response = _post(rest)
    assert response.status_code == 200, response.text
    assert response.json()["results"], response.text


def test_rest_store_failure_answers_5xx(rest) -> None:
    with patch(STORE_SEARCH, side_effect=STORE_FAILURE):
        response = _post(rest)
    assert response.status_code >= 500, response.text
    assert "index storage is unavailable" in response.text


def test_rest_search_timeout_answers_504(rest) -> None:
    with patch(STORE_SEARCH, side_effect=_timeout()):
        response = _post(rest)
    assert response.status_code == 504, response.text
    assert "timed out" in response.text


def test_rest_missing_provider_key_answers_explicit_5xx(rest) -> None:
    with patch(PROVIDER_FACTORY, side_effect=MISSING_KEY):
        response = _post(rest)
    assert response.status_code >= 500, response.text
    assert "VOYAGE_API_KEY" in response.text


def test_rest_client_validation_error_stays_4xx(rest) -> None:
    response = _post(rest, time_range="not-a-range")
    assert 400 <= response.status_code < 500, response.text


# ---------------------------------------------------------------------- MCP


def _mcp_body(
    repo: Path, tmp_path: Path, target: str, failure: BaseException
) -> Dict[str, Any]:
    """Run MCP search_code with ``target`` raising ``failure``; the failure
    is applied after the provider replacement so it is never overridden."""
    from code_indexer.server.mcp.handlers import search_code
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    repo_listing = MagicMock()
    repo_listing.list_activated_repositories.return_value = _user_repos(repo)
    repo_listing.user_has_activated_repo.return_value = True
    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "server-data"),
        activated_repo_manager=repo_listing,
        background_job_manager=MagicMock(),
    )
    app_stand_in = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        semantic_query_manager=manager,
        activated_repo_manager=repo_listing,
        golden_repo_manager=None,
    )
    params = {
        "query_text": QUERY,
        "repository_alias": REPO_ALIAS,
        "limit": 5,
        "min_score": 0.0,
        "search_mode": "semantic",
    }
    with (
        ranked_store_search(),
        patch("code_indexer.server.mcp.handlers._utils.app_module", app_stand_in),
        patch(target, side_effect=failure),
    ):
        response = search_code(params, ADMIN)
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return body


def test_mcp_store_failure_answers_error(env, tmp_path) -> None:
    _, repo = env
    body = _mcp_body(repo, tmp_path, STORE_SEARCH, STORE_FAILURE)
    assert body["success"] is False, body
    assert "index storage is unavailable" in body["error"]


def test_mcp_search_timeout_answers_timed_out_error(env, tmp_path) -> None:
    _, repo = env
    body = _mcp_body(repo, tmp_path, STORE_SEARCH, _timeout())
    assert body["success"] is False, body
    assert "timed out" in body["error"]


def test_mcp_missing_provider_key_answers_error(env, tmp_path) -> None:
    _, repo = env
    body = _mcp_body(repo, tmp_path, PROVIDER_FACTORY, MISSING_KEY)
    assert body["success"] is False, body
    assert "VOYAGE_API_KEY" in body["error"]
