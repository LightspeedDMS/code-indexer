"""Every search front door applies one client-error rule.

Only an intentional client error keeps its message; any other failure --
including a plain ``ValueError`` raised by storage or provider code --
answers a fixed public message and its detail reaches the server log only.

Real routes / handlers, real SemanticQueryManager, MultiSearchService and
FilesystemVectorStore over real git repos (the #2047 / #2109 fixtures).
Replaced: the embedding provider (external service), authentication, the
repository listing, and -- per test -- the call made to fail.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import SearchRequestError
from tests.unit.server.query import test_file_extensions_omni_2047 as omni_2047
from tests.unit.server.query.extension_filter_env_2047 import (
    LIMIT,
    QUERY,
    REPO_ALIAS,
    ranked_store_search,
)
from tests.unit.server.query.test_file_extensions_front_doors_2047 import ADMIN
from tests.unit.server.query.test_search_failure_bodies_multi_2109 import (
    _omni,
    _post_multi,
)
from tests.unit.server.query import test_search_failures_front_doors_2109 as fd_2109
from tests.unit.server.query.test_search_failures_front_doors_2109 import (
    SENTINEL,
    STORE_SEARCH,
    _logged,
    _post,
    _user_repos,
)

# The #2047 / #2109 fixtures, shared by binding them here.
omni_env = omni_2047.omni_env
multi_client = omni_2047.multi_client
env = fd_2109.env
rest = fd_2109.rest

LOGGER_NAME = "code_indexer.server"
INTERNAL_VALUE_ERROR = ValueError(f"cannot decode chunk stored at {SENTINEL}")
# Raises a ValueError straight out of the manager's search, unwrapped.
PERFORM_SEARCH = (
    "code_indexer.server.query.semantic_query_manager.SemanticQueryManager"
    "._perform_search"
)
FTS_OPEN = (
    "code_indexer.services.tantivy_index_manager.TantivyIndexManager.open_for_search"
)


def _mcp_search(
    repo: Path,
    tmp_path: Path,
    extra: Dict[str, Any],
    target: Optional[str] = None,
    failure: Optional[BaseException] = None,
) -> Dict[str, Any]:
    """Run MCP ``search_code`` on the activated repo with ``extra`` params."""
    from contextlib import ExitStack

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
    params: Dict[str, Any] = {
        "query_text": QUERY,
        "repository_alias": REPO_ALIAS,
        "limit": 5,
        "min_score": 0.0,
        "search_mode": "semantic",
    }
    params.update(extra)
    with ExitStack() as stack:
        stack.enter_context(ranked_store_search())
        stack.enter_context(
            patch("code_indexer.server.mcp.handlers._utils.app_module", app_stand_in)
        )
        if target is not None:
            stack.enter_context(patch(target, side_effect=failure))
        response = search_code(params, ADMIN)
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return body


# ----------------------------------------- internal ValueError: generic body


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_rest_query_internal_value_error_answers_generic_body(
    rest, caplog, mode
) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(PERFORM_SEARCH, side_effect=INTERNAL_VALUE_ERROR):
        response = _post(rest, search_mode=mode)
    assert response.status_code != 400, response.text
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_rest_query_fts_internal_value_error_answers_generic_body(
    rest, env, caplog
) -> None:
    _app, repo = env
    (Path(repo) / ".code-indexer" / "tantivy_index").mkdir(parents=True, exist_ok=True)
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(FTS_OPEN, side_effect=INTERNAL_VALUE_ERROR):
        response = _post(rest, search_mode="fts")
    assert response.status_code == 500, response.text
    assert "Search failed" in response.text
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_rest_multi_service_value_error_answers_generic_body(
    multi_client, caplog
) -> None:
    from code_indexer.server.routes import multi_query_routes

    client, repos = multi_client
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch.object(
        multi_query_routes.MultiSearchService,
        "search",
        side_effect=INTERNAL_VALUE_ERROR,
    ):
        response = _post_multi(client, repos)
    assert response.status_code == 500, response.text
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_rest_multi_client_error_keeps_its_message(multi_client) -> None:
    from code_indexer.server.routes import multi_query_routes

    client, repos = multi_client
    with patch.object(
        multi_query_routes.MultiSearchService,
        "search",
        side_effect=SearchRequestError("Unsupported search type: example-type"),
    ):
        response = _post_multi(client, repos)
    assert response.status_code == 422, response.text
    assert "Unsupported search type: example-type" in response.text


def test_rest_multi_repository_value_error_stays_per_repository(
    multi_client, caplog
) -> None:
    client, repos = multi_client
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(STORE_SEARCH, side_effect=INTERNAL_VALUE_ERROR):
        response = _post_multi(client, repos)
    assert response.status_code == 200, response.text
    errors = response.json()["errors"]
    assert set(errors) == set(repos), errors
    assert all("Search failed" in message for message in errors.values()), errors
    assert SENTINEL not in response.text


def test_mcp_search_code_internal_value_error_answers_generic_body(
    env, tmp_path, caplog
) -> None:
    _, repo = env
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    body = _mcp_search(repo, tmp_path, {}, STORE_SEARCH, INTERNAL_VALUE_ERROR)
    assert body["success"] is False, body
    assert "Search failed" in body["error"], body
    assert SENTINEL not in json.dumps(body)
    assert SENTINEL in _logged(caplog)


def test_mcp_omni_value_errors_answer_generic_messages(omni_env, caplog) -> None:
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    _, repos = omni_env
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(STORE_SEARCH, side_effect=INTERNAL_VALUE_ERROR):
        per_repo = _omni(repos)
    with patch.object(MultiSearchService, "search", side_effect=INTERNAL_VALUE_ERROR):
        service = _omni(repos)
    assert SENTINEL not in json.dumps(per_repo), per_repo
    assert SENTINEL not in json.dumps(service), service
    assert "Search failed" in json.dumps(service), service
    assert SENTINEL in _logged(caplog)


# ------------------------------------------- client messages keep their text


def test_rest_unknown_alias_keeps_its_message(rest) -> None:
    response = _post(rest, repository_alias="example-unknown-repo")
    assert 400 <= response.status_code < 500, response.text
    assert "example-unknown-repo" in response.text


def test_rest_invalid_time_range_keeps_its_message(rest) -> None:
    response = _post(rest, time_range="not-a-range")
    assert 400 <= response.status_code < 500, response.text
    assert "format" in response.text.lower(), response.text


@pytest.mark.parametrize(
    "extra, text",
    [
        ({"file_extensions": "py"}, "file_extensions must be a list"),
        ({"preferred_provider": "example-provider"}, "example-provider"),
        (
            {"query_strategy": "specific"},
            "preferred_provider required for specific strategy",
        ),
        ({"time_range": "not-a-range"}, "format"),
    ],
    ids=["file-extensions", "provider", "provider-required", "time-range"],
)
def test_mcp_client_validation_keeps_its_message(env, tmp_path, extra, text) -> None:
    _, repo = env
    body = _mcp_search(repo, tmp_path, extra)
    assert body["success"] is False, body
    assert text in body["error"], body


def test_reversed_time_range_keeps_its_message(rest, env, tmp_path) -> None:
    _, repo = env
    text = "End date must be after start date"
    response = _post(rest, time_range="2024-02-01..2024-01-01")
    assert 400 <= response.status_code < 500, response.text
    assert text in response.text, response.text
    body = _mcp_search(repo, tmp_path, {"time_range": "2024-02-01..2024-01-01"})
    assert body["success"] is False, body
    assert text in body["error"], body


def test_rest_multi_service_timeout_answers_504(multi_client, caplog) -> None:
    from code_indexer.server.routes import multi_query_routes

    client, repos = multi_client
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch.object(
        multi_query_routes.MultiSearchService,
        "search",
        side_effect=TimeoutError(f"deadline passed at {SENTINEL}"),
    ):
        response = _post_multi(client, repos)
    assert response.status_code == 504, response.text
    assert "Search timed out" in response.text
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_tracked_search_limit_check_is_a_client_error() -> None:
    from code_indexer.server.mcp.handlers.search import repo_search

    with pytest.raises(SearchRequestError, match="limit must be > 0"):
        repo_search._execute_tracked_search({}, ADMIN, [], 0)


def test_unsupported_search_type_is_a_client_error_per_repository() -> None:
    from code_indexer.server.multi.models import InternalMultiSearchRequest
    from code_indexer.server.multi.multi_search_config import MultiSearchConfig
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    service = MultiSearchService(MultiSearchConfig())
    request = InternalMultiSearchRequest.model_construct(
        repositories=["example-repo-a", "example-repo-b"],
        query=QUERY,
        search_type="example-type",
        limit=LIMIT,
        min_score=None,
        file_extensions=None,
        extension_deadline=None,
    )
    try:
        with pytest.raises(SearchRequestError, match="Unsupported search type"):
            service._search_single_repo_sync("example-repo-a", request)
        response = service._search_threaded(request)
    finally:
        service.shutdown()
    assert set(response.errors or {}) == {"example-repo-a", "example-repo-b"}
    assert all(
        "Unsupported search type" in message
        for message in (response.errors or {}).values()
    ), response.errors
