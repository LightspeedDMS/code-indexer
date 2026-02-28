"""Unit tests for groups router access control.

Story #318: Fix REST API Group Endpoints Leakage

Tests that list_groups and get_group REST endpoints require admin
authentication instead of allowing any authenticated user.

AC1: Non-admin users cannot see other groups' member lists -> 403 Forbidden
AC2: Non-admin users cannot see other groups' accessible repo lists -> 403
AC3: Non-admin users cannot enumerate admin usernames -> 403
AC4: Admin users retain full visibility -> unchanged behavior
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.groups import get_group as get_group_endpoint
from code_indexer.server.routers.groups import list_groups as list_groups_endpoint
from code_indexer.server.routers.groups import get_group_manager, set_group_manager


# ---------------------------------------------------------------------------
# Dependency signature inspection tests (AC1-AC3 structural guarantee)
# ---------------------------------------------------------------------------


class TestGroupEndpointDependencies:
    """Verify endpoints use get_current_admin_user dependency (not get_current_user)."""

    def test_list_groups_requires_admin_dependency(self):
        """list_groups must use get_current_admin_user, not get_current_user."""
        sig = inspect.signature(list_groups_endpoint)
        assert "current_user" in sig.parameters, (
            "list_groups must have a current_user parameter"
        )
        param = sig.parameters["current_user"]
        assert param.default.dependency is get_current_admin_user, (
            "list_groups must depend on get_current_admin_user, "
            f"got {param.default.dependency}"
        )

    def test_get_group_requires_admin_dependency(self):
        """get_group must use get_current_admin_user, not get_current_user."""
        sig = inspect.signature(get_group_endpoint)
        assert "current_user" in sig.parameters, (
            "get_group must have a current_user parameter"
        )
        param = sig.parameters["current_user"]
        assert param.default.dependency is get_current_admin_user, (
            "get_group must depend on get_current_admin_user, "
            f"got {param.default.dependency}"
        )

    def test_list_groups_does_not_use_regular_user_dependency(self):
        """list_groups must NOT use get_current_user (regular user dependency)."""
        sig = inspect.signature(list_groups_endpoint)
        param = sig.parameters["current_user"]
        assert param.default.dependency is not get_current_user, (
            "list_groups incorrectly uses get_current_user - security leak!"
        )

    def test_get_group_does_not_use_regular_user_dependency(self):
        """get_group must NOT use get_current_user (regular user dependency)."""
        sig = inspect.signature(get_group_endpoint)
        param = sig.parameters["current_user"]
        assert param.default.dependency is not get_current_user, (
            "get_group incorrectly uses get_current_user - security leak!"
        )


# ---------------------------------------------------------------------------
# Integration tests via FastAPI TestClient with dependency overrides
# ---------------------------------------------------------------------------


def _make_admin_user() -> User:
    return User(
        username="admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _make_regular_user() -> User:
    return User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _make_mock_group_manager():
    """Return a mock GroupAccessManager with pre-configured return values."""
    from code_indexer.server.services.group_access_manager import Group

    mock_manager = MagicMock()

    group = Group(
        id=1,
        name="developers",
        description="Dev group",
        is_default=False,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    mock_manager.get_all_groups.return_value = [group]
    mock_manager.get_group.return_value = group
    mock_manager.get_user_count_in_group.return_value = 2
    mock_manager.get_users_in_group.return_value = ["alice", "bob"]
    mock_manager.get_group_repos.return_value = ["repo-a", "repo-b"]

    return mock_manager


@pytest.fixture
def admin_client():
    """TestClient with admin user override and mock group manager."""
    from code_indexer.server.app import app

    admin_user = _make_admin_user()
    mock_manager = _make_mock_group_manager()

    app.dependency_overrides[get_current_admin_user] = lambda: admin_user
    app.dependency_overrides[get_group_manager] = lambda: mock_manager

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def non_admin_client():
    """TestClient with non-admin user override for get_current_user.

    This simulates a non-admin user hitting an endpoint that correctly
    requires get_current_admin_user.  We override get_current_user (the
    base dependency) to return a regular user; get_current_admin_user will
    still enforce the admin check by calling has_permission(), which will
    return False for a USER role and raise HTTP 403.
    """
    from code_indexer.server.app import app

    regular_user = _make_regular_user()
    mock_manager = _make_mock_group_manager()

    app.dependency_overrides[get_current_user] = lambda: regular_user
    app.dependency_overrides[get_group_manager] = lambda: mock_manager

    yield TestClient(app)

    app.dependency_overrides.clear()


class TestListGroupsEndpoint:
    """Tests for GET /api/v1/groups endpoint."""

    def test_list_groups_admin_succeeds(self, admin_client):
        """Admin user can list groups (AC4)."""
        response = admin_client.get("/api/v1/groups")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "developers"

    def test_list_groups_non_admin_rejected(self, non_admin_client):
        """Non-admin user receives 403 when listing groups (AC1, AC2, AC3)."""
        response = non_admin_client.get("/api/v1/groups")
        assert response.status_code == 403, (
            f"Expected 403 for non-admin user, got {response.status_code}. "
            "Group endpoint must restrict access to admins only."
        )

    def test_list_groups_unauthenticated_rejected(self):
        """Unauthenticated request receives 401 or 403."""
        from code_indexer.server.app import app

        mock_manager = _make_mock_group_manager()
        app.dependency_overrides[get_group_manager] = lambda: mock_manager

        client = TestClient(app)
        try:
            response = client.get("/api/v1/groups")
            assert response.status_code in (401, 403), (
                f"Unauthenticated request should return 401 or 403, got {response.status_code}"
            )
        finally:
            app.dependency_overrides.clear()


class TestGetGroupEndpoint:
    """Tests for GET /api/v1/groups/{group_id} endpoint."""

    def test_get_group_admin_succeeds(self, admin_client):
        """Admin user can retrieve group details including member list (AC4)."""
        response = admin_client.get("/api/v1/groups/1")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "developers"
        # AC4: admin sees full detail including user_ids and accessible_repos
        assert "user_ids" in data
        assert "accessible_repos" in data
        assert data["user_ids"] == ["alice", "bob"]
        assert data["accessible_repos"] == ["repo-a", "repo-b"]

    def test_get_group_non_admin_rejected(self, non_admin_client):
        """Non-admin user receives 403 when fetching group details (AC1, AC2, AC3)."""
        response = non_admin_client.get("/api/v1/groups/1")
        assert response.status_code == 403, (
            f"Expected 403 for non-admin user, got {response.status_code}. "
            "get_group endpoint must restrict member/repo info to admins only."
        )

    def test_get_group_unauthenticated_rejected(self):
        """Unauthenticated request receives 401 or non-200."""
        from code_indexer.server.app import app

        mock_manager = _make_mock_group_manager()
        app.dependency_overrides[get_group_manager] = lambda: mock_manager

        client = TestClient(app)
        try:
            response = client.get("/api/v1/groups/1")
            assert response.status_code in (401, 403), (
                f"Unauthenticated request should return 401 or 403, got {response.status_code}"
            )
        finally:
            app.dependency_overrides.clear()
