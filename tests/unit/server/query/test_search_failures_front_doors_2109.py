"""Search failures are reported at the server front doors, never answered
with an empty result, and never with internal detail in the body.

REST ``/api/query``: a store failure or a missing provider key answers 500
with a fixed public message, a search timeout 504 with a fixed public
message; a client validation error stays 4xx. MCP ``search_code``: the same
failures answer ``success: false`` with the same fixed messages. The full
failure detail reaches the server log only.

Real route / handler, real SemanticQueryManager, SemanticSearchService and
FilesystemVectorStore over a real git repo. Replaced: the embedding provider
(external service), authentication, the activated-repo listing, and -- per
test -- the store search call, made to fail the way storage or a deadline
does.
"""

from __future__ import annotations

import json
import logging
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
# Internal detail a failure may carry: it must reach the log, never a body.
SENTINEL = "/srv/sentinel-internal-path/index"
STORE_FAILURE = OSError(f"index storage is unavailable at {SENTINEL}")
MISSING_KEY = ValueError(f"VOYAGE_API_KEY is required ({SENTINEL})")
LOGGER_NAME = "code_indexer.server"
CORRELATION_ID = "corr-2109-example"


def _timeout() -> MultiIndexQueryTimeoutError:
    return MultiIndexQueryTimeoutError([SENTINEL], 5.0)


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record's message plus its exception text."""
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


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


def _post(client: TestClient, headers: Any = None, **extra: Any) -> Any:
    body: Dict[str, Any] = {
        "query_text": QUERY,
        "repository_alias": REPO_ALIAS,
        "limit": 5,
        "search_mode": "semantic",
    }
    body.update(extra)
    return client.post("/api/query", json=body, headers=headers)


# --------------------------------------------------------------------- REST


def test_rest_healthy_search_answers_200_with_results(rest) -> None:
    response = _post(rest)
    assert response.status_code == 200, response.text
    assert response.json()["results"], response.text


@pytest.mark.parametrize(
    "target, failure, status, message",
    [
        (STORE_SEARCH, STORE_FAILURE, 500, "Search failed"),
        (PROVIDER_FACTORY, MISSING_KEY, 500, "Search failed"),
        (STORE_SEARCH, _timeout(), 504, "Search timed out"),
    ],
    ids=["store-failure", "missing-provider-key", "timeout"],
)
def test_rest_failure_body_holds_no_internal_detail_and_log_does(
    rest, caplog, target, failure, status, message
) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(target, side_effect=failure):
        response = _post(rest)
    assert response.status_code == status, response.text
    assert message in response.text
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_rest_failure_body_carries_the_request_correlation_id(rest) -> None:
    with patch(STORE_SEARCH, side_effect=STORE_FAILURE):
        response = _post(rest, headers={"X-Correlation-ID": CORRELATION_ID})
    assert response.status_code == 500, response.text
    assert CORRELATION_ID in response.text
    assert SENTINEL not in response.text


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


@pytest.mark.parametrize(
    "target, failure, message",
    [
        (STORE_SEARCH, STORE_FAILURE, "Search failed"),
        (PROVIDER_FACTORY, MISSING_KEY, "Search failed"),
        (STORE_SEARCH, _timeout(), "Search timed out"),
    ],
    ids=["store-failure", "missing-provider-key", "timeout"],
)
def test_mcp_failure_body_holds_no_internal_detail_and_log_does(
    env, tmp_path, caplog, target, failure, message
) -> None:
    _, repo = env
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    body = _mcp_body(repo, tmp_path, target, failure)
    assert body["success"] is False, body
    assert message in body["error"], body
    assert SENTINEL not in json.dumps(body)
    assert SENTINEL in _logged(caplog)
