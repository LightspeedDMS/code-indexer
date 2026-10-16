"""Bug #1991: a chunk whose content cannot be read is flagged, never shown
as code, through the server front doors.

Real route / handler, real SemanticQueryManager and SemanticSearchService,
real FilesystemVectorStore over a real git repo (see
content_unavailable_env_1991). Replaced: the embedding provider (external
service), authentication, and the activated-repo listing (which repos the
caller has), so the query resolves to the test repository.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from tests.unit.server.query.content_unavailable_env_1991 import (
    BROKEN_FILE,
    GOOD_FILE,
    REPO_ALIAS,
    build_indexed_repo,
    by_path,
    real_store_search,
)

ADMIN = User(
    username="admin",
    password_hash="$2b$12$hash",
    role=UserRole.ADMIN,
    created_at=datetime.now(timezone.utc),
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # One provider configured -> deterministic primary-only routing.
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    return build_indexed_repo(tmp_path)


def _user_repos(repo: Path) -> List[Dict[str, Any]]:
    return [{"user_alias": REPO_ALIAS, "repo_path": str(repo)}]


def _assert_flagged(rows: List[Dict[str, Any]]) -> None:
    rows_by_path = by_path(rows)
    assert set(rows_by_path) == {GOOD_FILE, BROKEN_FILE}

    broken = rows_by_path[BROKEN_FILE]
    assert broken["content_unavailable"] is True
    assert broken["code_snippet"] == ""

    good = rows_by_path[GOOD_FILE]
    # Ordinary results are unchanged: the key is absent, not false.
    assert "content_unavailable" not in good
    assert "def image_generator" in good["code_snippet"]


@pytest.fixture
def client() -> TestClient:
    from code_indexer.server.app import create_app
    from code_indexer.server.auth.dependencies import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return TestClient(app)


def test_rest_api_query_flags_unreadable_chunk(client: TestClient, repo: Path) -> None:
    manager = client.app.state.semantic_query_manager  # type: ignore[attr-defined]

    with (
        real_store_search(),
        patch.object(
            manager.activated_repo_manager,
            "list_activated_repositories",
            return_value=_user_repos(repo),
        ),
    ):
        response = client.post(
            "/api/query",
            json={"query_text": "image generator", "limit": 10},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200, response.text
    _assert_flagged(response.json()["results"])


def test_rest_api_query_hybrid_flags_unreadable_chunk(
    client: TestClient, repo: Path
) -> None:
    # No FTS index -> hybrid degrades to semantic but still answers through
    # the unified hybrid response (QueryResultItem(**row).model_dump()).
    arm = client.app.state.semantic_query_manager.activated_repo_manager  # type: ignore[attr-defined]

    with (
        real_store_search(),
        patch.object(
            arm, "list_activated_repositories", return_value=_user_repos(repo)
        ),
        patch.object(arm, "get_activated_repo_path", return_value=str(repo)),
    ):
        response = client.post(
            "/api/query",
            json={
                "query_text": "image generator",
                "limit": 10,
                "search_mode": "hybrid",
            },
        )

    assert response.status_code == 200, response.text
    _assert_flagged(response.json()["semantic_results"])


def _mcp_rows(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    body = json.loads(response["content"][0]["text"])
    assert body["success"] is True, body
    rows: List[Dict[str, Any]] = body["results"]["results"]
    return rows


@pytest.mark.parametrize("query_strategy", ["primary_only", "failover", "parallel"])
def test_mcp_search_code_flags_unreadable_chunk(
    repo: Path, tmp_path: Path, query_strategy: str
) -> None:
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

    with (
        real_store_search(),
        patch("code_indexer.server.mcp.handlers._utils.app_module", app_stand_in),
    ):
        response = search_code(
            {
                "query_text": "image generator",
                "repository_alias": REPO_ALIAS,
                "limit": 10,
                "min_score": 0.0,
                "query_strategy": query_strategy,
            },
            ADMIN,
        )

    _assert_flagged(_mcp_rows(response))
