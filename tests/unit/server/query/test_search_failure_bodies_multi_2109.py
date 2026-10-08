"""Multi-repository search failures never put internal detail in a body.

REST ``/api/query/multi`` and MCP ``search_code`` with a list of aliases
(omni) answer a repository's internal failure, and a failure of the whole
multi-repository service, with a fixed public message; the failure detail
reaches the server log only.

Real route / handler, real MultiSearchService, SemanticSearchService and
FilesystemVectorStore over two real git repos (the #2047 omni fixtures).
Replaced: the embedding provider (external service), authentication, the
alias -> path lookup, and -- per test -- the store search call or the
service's search entry point, made to fail.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict
from unittest.mock import patch

import pytest

from tests.unit.server.query.extension_filter_env_2047 import (
    LIMIT,
    QUERY,
    ranked_store_search,
)
from tests.unit.server.query import test_file_extensions_omni_2047 as omni_2047
from tests.unit.server.query.test_file_extensions_omni_2047 import ADMIN, _registry

# The #2047 two-repository fixtures, shared by binding them here.
omni_env = omni_2047.omni_env
multi_client = omni_2047.multi_client

STORE_SEARCH = (
    "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore.search"
)
SENTINEL = "/srv/sentinel-internal-path/index"
STORE_FAILURE = OSError(f"index storage is unavailable at {SENTINEL}")
LOGGER_NAME = "code_indexer.server"


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


def _post_multi(client: Any, repos: Dict) -> Any:
    return client.post(
        "/api/query/multi",
        json={
            "repositories": list(repos),
            "query": QUERY,
            "search_type": "semantic",
            "limit": LIMIT,
        },
    )


def _omni(repos: Dict) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers import search_code
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    MultiSearchService._reset_singleton()
    try:
        with ranked_store_search(), _registry(repos):
            response = search_code(
                {
                    "query_text": QUERY,
                    "repository_alias": list(repos),
                    "limit": LIMIT * len(repos),
                    "search_mode": "semantic",
                    "aggregation_mode": "per_repo",
                    "response_format": "flat",
                },
                ADMIN,
            )
    finally:
        MultiSearchService._reset_singleton()
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return body


def test_rest_multi_repository_failure_holds_no_internal_detail(
    multi_client, caplog
) -> None:
    client, repos = multi_client
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(STORE_SEARCH, side_effect=STORE_FAILURE):
        response = _post_multi(client, repos)
    assert response.status_code == 200, response.text
    errors = response.json()["errors"]
    assert set(errors) == set(repos), errors
    assert all("Search failed" in message for message in errors.values()), errors
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_rest_multi_service_failure_answers_500_without_internal_detail(
    multi_client, caplog
) -> None:
    from code_indexer.server.routes import multi_query_routes

    client, repos = multi_client
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch.object(
        multi_query_routes.MultiSearchService,
        "search",
        side_effect=RuntimeError(f"pool failure at {SENTINEL}"),
    ):
        response = _post_multi(client, repos)
    assert response.status_code == 500, response.text
    assert "search failed" in response.text.lower()
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


def test_mcp_omni_repository_failure_holds_no_internal_detail(omni_env, caplog) -> None:
    _, repos = omni_env
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(STORE_SEARCH, side_effect=STORE_FAILURE):
        body = _omni(repos)
    assert SENTINEL not in json.dumps(body), body
    assert SENTINEL in _logged(caplog)


def test_mcp_omni_service_failure_holds_no_internal_detail(omni_env, caplog) -> None:
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    _, repos = omni_env
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    with patch.object(
        MultiSearchService,
        "search",
        side_effect=RuntimeError(f"pool failure at {SENTINEL}"),
    ):
        body = _omni(repos)
    assert SENTINEL not in json.dumps(body), body
    assert "Search failed" in json.dumps(body), body
    assert SENTINEL in _logged(caplog)
