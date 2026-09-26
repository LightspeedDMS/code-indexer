"""Regression tests: POST /api/query/multi must enforce repo-level
authorization across all four search types (semantic, FTS, regex,
temporal).

Front door: real FastAPI TestClient against multi_query_routes.router, with a
REAL AccessFilteringService backed by a REAL GroupAccessManager (temp SQLite
DB) -- not a mock of the access-control decision itself. The underlying
MultiSearchService is mocked (its own search execution is not what this test
targets), and the test asserts it is NEVER invoked when access is denied --
proving no partial/leaked results reach the search backend at all.

Scenarios (parametrized across all four search_type values):
- normal_user without one of the two requested repos' group grant -> 403,
  search() never called
- admin user bypasses the check entirely -> 200, search() called
- normal_user WITH both requested repos' group grant -> 200, search() called
- access_filtering_service missing from app.state -> fails closed, never 200
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.multi.models import MultiSearchMetadata, MultiSearchResponse
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.routes import multi_query_routes


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def group_db_path() -> Iterator[Path]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _build_access_service(
    db_path: Path,
    *,
    granted_username: str,
    granted_repos: list,
    admin_username: str = "admin_user",
) -> AccessFilteringService:
    """Build a REAL AccessFilteringService with a REAL GroupAccessManager."""
    gam = GroupAccessManager(db_path)
    group = gam.create_group("restricted", "test group")
    gam.assign_user_to_group(granted_username, group.id, assigned_by="test")
    for repo in granted_repos:
        gam.grant_repo_access(repo, group.id, granted_by="test")

    admins_group = gam.get_group_by_name("admins")
    assert admins_group is not None, "bootstrap must create the 'admins' group"
    gam.assign_user_to_group(admin_username, admins_group.id, assigned_by="test")

    return AccessFilteringService(gam)


def _build_app(user: User, access_service) -> FastAPI:
    app = FastAPI()
    app.include_router(multi_query_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    if access_service is not None:
        app.state.access_filtering_service = access_service
    return app


def _canned_response() -> MultiSearchResponse:
    return MultiSearchResponse(
        results={"example-repo": [], "other-repo": []},
        metadata=MultiSearchMetadata(
            total_results=0, total_repos_searched=2, execution_time_ms=1
        ),
        errors=None,
    )


SEARCH_TYPES = ["semantic", "fts", "regex", "temporal"]


class TestMultiQueryRepoAuthz:
    @pytest.mark.parametrize("search_type", SEARCH_TYPES)
    def test_denied_for_ungranted_repo_returns_403_no_search_call(
        self, group_db_path, search_type
    ):
        """normal_user whose group lacks 'other-repo' gets 403 for ALL four
        search types, and the search backend is never invoked."""
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        mock_service = MagicMock()
        mock_service.search.return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            "query": "authentication",
            "search_type": search_type,
        }
        with patch.object(
            multi_query_routes,
            "get_multi_search_service",
            return_value=mock_service,
        ):
            resp = client.post("/api/query/multi", json=body)

        assert resp.status_code == 403
        mock_service.search.assert_not_called()

    @pytest.mark.parametrize("search_type", SEARCH_TYPES)
    def test_admin_bypasses_check(self, group_db_path, search_type):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin, access_service)
        client = TestClient(app)

        mock_service = MagicMock()
        mock_service.search.return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            "query": "authentication",
            "search_type": search_type,
        }
        with patch.object(
            multi_query_routes,
            "get_multi_search_service",
            return_value=mock_service,
        ):
            resp = client.post("/api/query/multi", json=body)

        assert resp.status_code == 200
        mock_service.search.assert_called_once()

    @pytest.mark.parametrize("search_type", SEARCH_TYPES)
    def test_granted_user_succeeds(self, group_db_path, search_type):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo", "other-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        mock_service = MagicMock()
        mock_service.search.return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            "query": "authentication",
            "search_type": search_type,
        }
        with patch.object(
            multi_query_routes,
            "get_multi_search_service",
            return_value=mock_service,
        ):
            resp = client.post("/api/query/multi", json=body)

        assert resp.status_code == 200
        mock_service.search.assert_called_once()

    def test_access_filtering_service_unavailable_fails_closed(self, group_db_path):
        user = _make_user("some_user")
        app = _build_app(user, access_service=None)
        client = TestClient(app)

        mock_service = MagicMock()
        mock_service.search.return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            "query": "authentication",
            "search_type": "semantic",
        }
        with patch.object(
            multi_query_routes,
            "get_multi_search_service",
            return_value=mock_service,
        ):
            resp = client.post("/api/query/multi", json=body)

        assert resp.status_code == 500
        mock_service.search.assert_not_called()
