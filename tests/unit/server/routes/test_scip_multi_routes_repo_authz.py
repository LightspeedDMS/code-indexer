"""Regression tests: /api/scip/multi/* (definition, references,
dependencies, dependents, callchain) must enforce repo-level authorization,
matching the singular-repo scip_queries.py router (Story #704).

Front door: real FastAPI TestClient against scip_multi_routes.router, with a
REAL AccessFilteringService backed by a REAL GroupAccessManager (temp SQLite
DB) -- not a mock of the access-control decision itself. SCIPMultiService is
mocked (its own SCIP query execution is not what this test targets), and the
test asserts the mocked service method is NEVER invoked when access is
denied -- proving no partial/leaked results reach the search backend.

Scenarios (parametrized across all five routes):
- normal_user without one of the two requested repos' group grant -> 403,
  service method never called
- admin user bypasses the check entirely -> 200, service method called
- normal_user WITH both requested repos' group grant -> 200, service
  method called
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
from code_indexer.server.multi.scip_models import SCIPMultiMetadata, SCIPMultiResponse
from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.routes import scip_multi_routes


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
    app.include_router(scip_multi_routes.router)
    app.dependency_overrides[get_current_user] = lambda: user
    if access_service is not None:
        app.state.access_filtering_service = access_service
    return app


def _canned_response() -> SCIPMultiResponse:
    return SCIPMultiResponse(
        results={"example-repo": [], "other-repo": []},
        metadata=SCIPMultiMetadata(
            total_results=0,
            repos_searched=2,
            repos_with_results=0,
            execution_time_ms=1,
        ),
        skipped={},
        errors={},
    )


# (route path, request body, mocked-service-method-name)
ROUTES = [
    ("/api/scip/multi/definition", {"symbol": "com.example.Foo"}, "definition"),
    ("/api/scip/multi/references", {"symbol": "com.example.Foo"}, "references"),
    ("/api/scip/multi/dependencies", {"symbol": "com.example.Foo"}, "dependencies"),
    ("/api/scip/multi/dependents", {"symbol": "com.example.Foo"}, "dependents"),
    (
        "/api/scip/multi/callchain",
        {"symbol": "com.example.Foo", "from_symbol": "a", "to_symbol": "b"},
        "callchain",
    ),
]


class TestScipMultiRepoAuthz:
    @pytest.mark.parametrize("path,extra_body,method_name", ROUTES)
    def test_denied_for_ungranted_repo_returns_403_no_service_call(
        self, group_db_path, path, extra_body, method_name
    ):
        user = _make_user("example_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        mock_service = MagicMock()
        getattr(mock_service, method_name).return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            **extra_body,
        }
        with patch.object(
            scip_multi_routes,
            "get_scip_multi_service",
            return_value=mock_service,
        ):
            resp = client.post(path, json=body)

        assert resp.status_code == 403, f"{path} did not return 403: {resp.text}"
        getattr(mock_service, method_name).assert_not_called()

    @pytest.mark.parametrize("path,extra_body,method_name", ROUTES)
    def test_admin_bypasses_check(self, group_db_path, path, extra_body, method_name):
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _build_access_service(
            group_db_path, granted_username="someone_else", granted_repos=[]
        )
        app = _build_app(admin, access_service)
        client = TestClient(app)

        mock_service = MagicMock()
        getattr(mock_service, method_name).return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            **extra_body,
        }
        with patch.object(
            scip_multi_routes,
            "get_scip_multi_service",
            return_value=mock_service,
        ):
            resp = client.post(path, json=body)

        assert resp.status_code == 200, f"{path} did not return 200: {resp.text}"
        getattr(mock_service, method_name).assert_called_once()

    @pytest.mark.parametrize("path,extra_body,method_name", ROUTES)
    def test_granted_user_succeeds(self, group_db_path, path, extra_body, method_name):
        user = _make_user("granted_user")
        access_service = _build_access_service(
            group_db_path,
            granted_username=user.username,
            granted_repos=["example-repo", "other-repo"],
        )
        app = _build_app(user, access_service)
        client = TestClient(app)

        mock_service = MagicMock()
        getattr(mock_service, method_name).return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            **extra_body,
        }
        with patch.object(
            scip_multi_routes,
            "get_scip_multi_service",
            return_value=mock_service,
        ):
            resp = client.post(path, json=body)

        assert resp.status_code == 200, f"{path} did not return 200: {resp.text}"
        getattr(mock_service, method_name).assert_called_once()

    def test_access_filtering_service_unavailable_fails_closed(self, group_db_path):
        user = _make_user("some_user")
        app = _build_app(user, access_service=None)
        client = TestClient(app)

        mock_service = MagicMock()
        mock_service.definition.return_value = _canned_response()

        body = {
            "repositories": ["example-repo-global", "other-repo-global"],
            "symbol": "com.example.Foo",
        }
        with patch.object(
            scip_multi_routes,
            "get_scip_multi_service",
            return_value=mock_service,
        ):
            resp = client.post("/api/scip/multi/definition", json=body)

        assert resp.status_code == 500
        mock_service.definition.assert_not_called()
