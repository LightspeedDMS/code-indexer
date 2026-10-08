"""Search front doors expose only client-error text.

One shared classifier decides what a failed search may say to a client:
request-validation errors, deliberate 4xx HTTP errors and a named
repository-not-found error keep their text; every other exception answers
a fixed public message and its detail reaches the server log only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from code_indexer.server.query.search_error_policy import classify_search_error
from code_indexer.server.query.semantic_query_manager import (
    SearchRepositoryNotFoundError,
    SearchRequestError,
    SemanticQueryError,
)
from tests.unit.server.query.test_search_failures_front_doors_2109 import (  # noqa: F401
    SENTINEL,
    _logged,
    _mcp_body,
    _post,
    env,
    rest,
)

LOGGER_NAME = "code_indexer.server"


# --------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    "error",
    [
        OSError(f"storage unavailable at {SENTINEL}"),
        FileNotFoundError(f"missing {SENTINEL}"),
        SemanticQueryError(f"FTS search failed: {SENTINEL}"),
        RuntimeError(SENTINEL),
        HTTPException(status_code=500, detail=SENTINEL),
    ],
    ids=["oserror", "filenotfound", "semantic-wrapper", "runtime", "http-500"],
)
def test_internal_errors_answer_fixed_message(error: Exception) -> None:
    outcome = classify_search_error(error)
    assert not outcome.client_error
    assert SENTINEL not in outcome.message
    assert "Search failed" in outcome.message


def test_timeout_in_chain_answers_timed_out() -> None:
    try:
        try:
            raise TimeoutError(SENTINEL)
        except TimeoutError as inner:
            raise SemanticQueryError(f"Query timed out: {inner}") from inner
    except SemanticQueryError as wrapped:
        outcome = classify_search_error(wrapped)
    assert outcome.timed_out and not outcome.client_error
    assert "Search timed out" in outcome.message
    assert SENTINEL not in outcome.message


@pytest.mark.parametrize(
    "error, text",
    [
        (SearchRequestError("Limit must be greater than 0"), "Limit must be"),
        (SearchRepositoryNotFoundError("example-repo"), "example-repo"),
        (ValueError("Invalid time range format"), "Invalid time range"),
        (HTTPException(status_code=404, detail="Not here"), "Not here"),
    ],
    ids=["request-error", "repo-not-found", "validation", "http-4xx"],
)
def test_client_errors_keep_their_text(error: Exception, text: str) -> None:
    outcome = classify_search_error(error)
    assert outcome.client_error
    assert text in outcome.message


def test_repository_not_found_names_only_the_alias() -> None:
    error = SearchRepositoryNotFoundError("example-repo-global")
    assert str(error) == "Repository 'example-repo-global' not found"


# --------------------------------------------------------------------- REST


def test_rest_fts_failure_body_holds_no_internal_detail(
    rest,  # noqa: F811 -- pytest fixture
    env,  # noqa: F811 -- pytest fixture
    caplog,
) -> None:
    _app, repo = env
    (Path(repo) / ".code-indexer" / "tantivy_index").mkdir(parents=True, exist_ok=True)
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    with patch(
        "code_indexer.services.tantivy_index_manager.TantivyIndexManager.open_for_search",
        side_effect=OSError(f"index unreadable at {SENTINEL}"),
    ):
        response = _post(rest, search_mode="fts")
    assert response.status_code == 500, response.text
    assert "Search failed" in response.text
    assert SENTINEL not in response.text
    assert SENTINEL in _logged(caplog)


# ---------------------------------------------------------------------- MCP


def test_mcp_unclassified_error_body_holds_no_internal_detail(
    env,  # noqa: F811 -- pytest fixture
    tmp_path: Path,
) -> None:
    _app, repo = env
    body = _mcp_body(
        repo,
        tmp_path,
        "code_indexer.storage.filesystem_vector_store.FilesystemVectorStore.search",
        FileNotFoundError(f"index missing at {SENTINEL}"),
    )
    assert body["success"] is False
    assert SENTINEL not in json.dumps(body)
    assert "Search failed" in body["error"]


def test_mcp_global_alias_with_missing_target_gives_clean_not_found(
    tmp_path: Path,
) -> None:
    from code_indexer.server.mcp.handlers.search import repo_search
    from tests.unit.server.query.test_file_extensions_front_doors_2047 import ADMIN

    missing = tmp_path / "sentinel-internal-path" / "gone"
    entry = {"alias_name": "example-repo-global", "index_path": str(missing)}
    with (
        patch.object(repo_search, "_get_golden_repos_dir", return_value=str(tmp_path)),
        patch.object(repo_search, "_list_global_repos", return_value=[entry]),
        patch(
            "code_indexer.global_repos.alias_manager.resolve_alias_or_index_path",
            return_value=str(missing),
        ),
        pytest.raises(SearchRepositoryNotFoundError) as raised,
    ):
        repo_search._resolve_global_repo_target("example-repo-global", ADMIN)
    assert "sentinel-internal-path" not in str(raised.value)
    assert "example-repo-global" in str(raised.value)
