"""
Route-level coverage tests for 4 previously-uncovered routes in groups.py router.

These are safety-net tests written before refactoring inline_routes.py.
They verify each route is registered, accepts the correct HTTP methods,
and returns expected responses.

Routes covered:
- POST /api/v1/groups/{group_id}/members   -- assign user to group
- POST /api/v1/groups/{group_id}/repos     -- add repo(s) to group (single + bulk)
- DELETE /api/v1/groups/{group_id}/repos/{repo_name}  -- remove single repo
- DELETE /api/v1/groups/{group_id}/repos   -- bulk remove repos
"""

import inspect
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_user,
)
from code_indexer.server.routers.groups import (
    add_repo_to_group,
    assign_user_to_group,
    bulk_remove_repos_from_group,
    get_group_manager,
    remove_repo_from_group,
    router,
    set_group_manager,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager

NONEXISTENT_GROUP_ID = 99999
CIDX_META = "cidx-meta"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
    """Temporary SQLite database file, cleaned up after each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    """Real GroupAccessManager backed by a temporary database."""
    return GroupAccessManager(temp_db_path)


@pytest.fixture
def mock_admin_user():
    """Minimal mock that satisfies the admin-user dependency."""
    user = MagicMock()
    user.username = "admin_user"
    user.role = "admin"
    return user


@pytest.fixture
def test_client(group_manager, mock_admin_user):
    """FastAPI TestClient with real GroupAccessManager and overridden auth deps."""
    app = FastAPI()
    app.include_router(router)
    set_group_manager(group_manager)
    app.dependency_overrides[get_current_admin_user] = lambda: mock_admin_user
    app.dependency_overrides[get_current_user] = lambda: mock_admin_user
    app.dependency_overrides[get_group_manager] = lambda: group_manager
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Structural / dependency-signature tests (route registration guard)
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Verify that all four routes are registered with correct HTTP verbs.

    Note: Must iterate router.routes directly (not convert to dict)
    because multiple routes can share the same path with different methods.
    """

    def test_assign_user_to_group_is_registered_as_post(self):
        """POST /{group_id}/members must be registered on the router."""
        assert any(
            "members" in r.path and "POST" in (r.methods or set())
            for r in router.routes
        ), "POST /{group_id}/members not found on router"

    def test_add_repo_to_group_is_registered_as_post(self):
        """POST /{group_id}/repos must be registered on the router."""
        assert any(
            r.path.endswith("/repos") and "POST" in (r.methods or set())
            for r in router.routes
        ), "POST /{group_id}/repos not found on router"

    def test_remove_repo_from_group_is_registered_as_delete(self):
        """DELETE /{group_id}/repos/{repo_name} must be registered on the router."""
        assert any(
            "repo_name" in r.path and "DELETE" in (r.methods or set())
            for r in router.routes
        ), "DELETE /{group_id}/repos/{repo_name} not found on router"

    def test_bulk_remove_repos_is_registered_as_delete(self):
        """DELETE /{group_id}/repos must be registered on the router."""
        assert any(
            r.path.endswith("/repos") and "DELETE" in (r.methods or set())
            for r in router.routes
        ), "DELETE /{group_id}/repos (bulk) not found on router"


class TestEndpointDependencies:
    """Verify all four endpoint functions use get_current_admin_user."""

    def _get_admin_dep(self, fn):
        sig = inspect.signature(fn)
        param = sig.parameters.get("current_user")
        assert param is not None, f"{fn.__name__} has no current_user parameter"
        return param.default.dependency

    def test_assign_user_to_group_requires_admin(self):
        dep = self._get_admin_dep(assign_user_to_group)
        assert dep is get_current_admin_user

    def test_add_repo_to_group_requires_admin(self):
        dep = self._get_admin_dep(add_repo_to_group)
        assert dep is get_current_admin_user

    def test_remove_repo_from_group_requires_admin(self):
        dep = self._get_admin_dep(remove_repo_from_group)
        assert dep is get_current_admin_user

    def test_bulk_remove_repos_requires_admin(self):
        dep = self._get_admin_dep(bulk_remove_repos_from_group)
        assert dep is get_current_admin_user


# ---------------------------------------------------------------------------
# POST /api/v1/groups/{group_id}/members
# ---------------------------------------------------------------------------


class TestAssignUserToGroup:
    """Tests for POST /api/v1/groups/{group_id}/members."""

    def test_assign_user_returns_200(self, test_client, group_manager):
        """Successful assignment returns HTTP 200."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={"user_id": "alice"},
        )
        assert response.status_code == 200

    def test_assign_user_returns_message(self, test_client, group_manager):
        """Response body contains a human-readable message."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={"user_id": "bob"},
        )
        data = response.json()
        assert "message" in data
        assert "bob" in data["message"]

    def test_assign_user_actually_assigns_to_group(self, test_client, group_manager):
        """After a successful call the user is a member of the group."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={"user_id": "carol"},
        )
        members = group_manager.get_users_in_group(admins.id)
        assert "carol" in members

    def test_assign_user_to_nonexistent_group_returns_404(self, test_client):
        """Assigning to a group that does not exist returns HTTP 404."""
        response = test_client.post(
            f"/api/v1/groups/{NONEXISTENT_GROUP_ID}/members",
            json={"user_id": "ghost"},
        )
        assert response.status_code == 404

    def test_assign_user_empty_user_id_returns_422(self, test_client, group_manager):
        """Empty user_id fails Pydantic validation with HTTP 422."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={"user_id": ""},
        )
        assert response.status_code == 422

    def test_assign_user_missing_user_id_returns_422(self, test_client, group_manager):
        """Missing user_id field fails Pydantic validation with HTTP 422."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={},
        )
        assert response.status_code == 422

    def test_assign_user_records_assigned_by(self, test_client, group_manager):
        """Assignment records which admin performed the action."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={"user_id": "dave"},
        )
        membership = group_manager.get_user_membership("dave")
        assert membership is not None
        assert membership.assigned_by == "admin_user"

    def test_assign_user_to_different_group_moves_membership(
        self, test_client, group_manager
    ):
        """Reassigning a user to a new group replaces the old membership."""
        admins = group_manager.get_group_by_name("admins")
        users = group_manager.get_group_by_name("users")

        test_client.post(
            f"/api/v1/groups/{admins.id}/members",
            json={"user_id": "eve"},
        )
        test_client.post(
            f"/api/v1/groups/{users.id}/members",
            json={"user_id": "eve"},
        )

        group = group_manager.get_user_group("eve")
        assert group is not None
        assert group.id == users.id


# ---------------------------------------------------------------------------
# POST /api/v1/groups/{group_id}/repos  (single and bulk)
# ---------------------------------------------------------------------------


class TestAddRepoToGroup:
    """Tests for POST /api/v1/groups/{group_id}/repos."""

    def test_add_single_repo_returns_201(self, test_client, group_manager):
        """Adding a new single repo returns HTTP 201."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "my-service"},
        )
        assert response.status_code == 201

    def test_add_single_repo_response_body(self, test_client, group_manager):
        """Response body contains added count and message."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "service-a"},
        )
        data = response.json()
        assert data["added"] == 1
        assert "message" in data

    def test_add_single_repo_grants_access(self, test_client, group_manager):
        """After the call the repo appears in the group's access list."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "backend"},
        )
        repos = group_manager.get_group_repos(admins.id)
        assert "backend" in repos

    def test_add_duplicate_repo_returns_200(self, test_client, group_manager):
        """Adding an already-accessible repo is idempotent and returns 200."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "idempotent-repo"},
        )
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "idempotent-repo"},
        )
        assert response.status_code == 200

    def test_add_duplicate_repo_added_count_zero(self, test_client, group_manager):
        """Idempotent add returns added=0."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "already-there"},
        )
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "already-there"},
        )
        assert response.json()["added"] == 0

    def test_add_bulk_repos_returns_201(self, test_client, group_manager):
        """Bulk add of new repos returns HTTP 201."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["repo-x", "repo-y", "repo-z"]},
        )
        assert response.status_code == 201

    def test_add_bulk_repos_count(self, test_client, group_manager):
        """Bulk add reports the correct number of newly added repos."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["bulk-a", "bulk-b"]},
        )
        assert response.json()["added"] == 2

    def test_add_bulk_repos_grants_all_access(self, test_client, group_manager):
        """All repos in the bulk list gain access."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["svc-1", "svc-2", "svc-3"]},
        )
        repos = group_manager.get_group_repos(admins.id)
        assert "svc-1" in repos
        assert "svc-2" in repos
        assert "svc-3" in repos

    def test_add_repos_to_nonexistent_group_returns_404(self, test_client):
        """Adding repos to a nonexistent group returns HTTP 404."""
        response = test_client.post(
            f"/api/v1/groups/{NONEXISTENT_GROUP_ID}/repos",
            json={"repo_name": "any-repo"},
        )
        assert response.status_code == 404

    def test_add_repos_missing_both_fields_returns_422(
        self, test_client, group_manager
    ):
        """Omitting both repo_name and repos fails validation with HTTP 422."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={},
        )
        assert response.status_code == 422

    def test_add_repos_empty_repos_list_returns_422(self, test_client, group_manager):
        """Providing an empty repos list fails validation with HTTP 422."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": []},
        )
        assert response.status_code == 422

    def test_add_repos_records_granted_by(self, test_client, group_manager):
        """Access record stores the admin username from the auth token."""
        admins = group_manager.get_group_by_name("admins")
        test_client.post(
            f"/api/v1/groups/{admins.id}/repos",
            json={"repo_name": "tracked-repo"},
        )
        record = group_manager.get_repo_access("tracked-repo", admins.id)
        assert record is not None
        assert record.granted_by == "admin_user"


# ---------------------------------------------------------------------------
# DELETE /api/v1/groups/{group_id}/repos/{repo_name}
# ---------------------------------------------------------------------------


class TestRemoveRepoFromGroup:
    """Tests for DELETE /api/v1/groups/{group_id}/repos/{repo_name}."""

    def test_remove_existing_repo_returns_204(self, test_client, group_manager):
        """Removing an accessible repo returns HTTP 204 No Content."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("to-remove", admins.id, "admin")
        response = test_client.delete(f"/api/v1/groups/{admins.id}/repos/to-remove")
        assert response.status_code == 204

    def test_remove_repo_actually_removes_access(self, test_client, group_manager):
        """After deletion the repo is no longer in the group's access list."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("gone-repo", admins.id, "admin")
        test_client.delete(f"/api/v1/groups/{admins.id}/repos/gone-repo")
        repos = group_manager.get_group_repos(admins.id)
        assert "gone-repo" not in repos

    def test_remove_nonexistent_repo_returns_404(self, test_client, group_manager):
        """Removing a repo not in the access list returns HTTP 404."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.delete(
            f"/api/v1/groups/{admins.id}/repos/does-not-exist"
        )
        assert response.status_code == 404

    def test_remove_cidx_meta_returns_400(self, test_client, group_manager):
        """Attempting to revoke cidx-meta access returns HTTP 400."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.delete(f"/api/v1/groups/{admins.id}/repos/{CIDX_META}")
        assert response.status_code == 400

    def test_remove_cidx_meta_error_message(self, test_client, group_manager):
        """400 response for cidx-meta mentions the protected repo name."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.delete(f"/api/v1/groups/{admins.id}/repos/{CIDX_META}")
        assert CIDX_META in response.json()["detail"].lower()

    def test_remove_from_nonexistent_group_returns_404(self, test_client):
        """Removing a repo from a nonexistent group returns HTTP 404."""
        response = test_client.delete(
            f"/api/v1/groups/{NONEXISTENT_GROUP_ID}/repos/any-repo"
        )
        assert response.status_code == 404

    def test_remove_repo_204_body_is_empty(self, test_client, group_manager):
        """Successful 204 response has no body content."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("empty-body-repo", admins.id, "admin")
        response = test_client.delete(
            f"/api/v1/groups/{admins.id}/repos/empty-body-repo"
        )
        assert response.status_code == 204
        assert response.content == b""


# ---------------------------------------------------------------------------
# DELETE /api/v1/groups/{group_id}/repos  (bulk remove)
# ---------------------------------------------------------------------------


class TestBulkRemoveReposFromGroup:
    """Tests for DELETE /api/v1/groups/{group_id}/repos (bulk)."""

    def test_bulk_remove_returns_200(self, test_client, group_manager):
        """Bulk remove returns HTTP 200 with a count."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("del-a", admins.id, "admin")
        group_manager.grant_repo_access("del-b", admins.id, "admin")
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["del-a", "del-b"]},
        )
        assert response.status_code == 200

    def test_bulk_remove_returns_removed_count(self, test_client, group_manager):
        """Response body reports how many repos were actually removed."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("cnt-a", admins.id, "admin")
        group_manager.grant_repo_access("cnt-b", admins.id, "admin")
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["cnt-a", "cnt-b"]},
        )
        assert response.json()["removed"] == 2

    def test_bulk_remove_actually_removes_repos(self, test_client, group_manager):
        """All listed repos are removed from the group's access list."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("rm-1", admins.id, "admin")
        group_manager.grant_repo_access("rm-2", admins.id, "admin")
        test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["rm-1", "rm-2"]},
        )
        repos = group_manager.get_group_repos(admins.id)
        assert "rm-1" not in repos
        assert "rm-2" not in repos

    def test_bulk_remove_nonexistent_repos_removed_count_zero(
        self, test_client, group_manager
    ):
        """Removing repos that were never granted returns removed=0."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["never-existed-a", "never-existed-b"]},
        )
        assert response.status_code == 200
        assert response.json()["removed"] == 0

    def test_bulk_remove_skips_cidx_meta_silently(self, test_client, group_manager):
        """cidx-meta in the bulk list is silently skipped (not an error)."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("removable", admins.id, "admin")
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": [CIDX_META, "removable"]},
        )
        assert response.status_code == 200
        assert response.json()["removed"] == 1

    def test_bulk_remove_cidx_meta_still_accessible(self, test_client, group_manager):
        """cidx-meta remains accessible after bulk remove that included it."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("other", admins.id, "admin")
        test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": [CIDX_META, "other"]},
        )
        repos = group_manager.get_group_repos(admins.id)
        assert CIDX_META in repos

    def test_bulk_remove_from_nonexistent_group_returns_404(self, test_client):
        """Bulk remove against a nonexistent group returns HTTP 404."""
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{NONEXISTENT_GROUP_ID}/repos",
            json={"repos": ["any-repo"]},
        )
        assert response.status_code == 404

    def test_bulk_remove_missing_repos_field_returns_422(
        self, test_client, group_manager
    ):
        """Omitting the repos field fails Pydantic validation with HTTP 422."""
        admins = group_manager.get_group_by_name("admins")
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={},
        )
        assert response.status_code == 422

    def test_bulk_remove_partial_success(self, test_client, group_manager):
        """When only some repos in the list exist, only existing ones are removed."""
        admins = group_manager.get_group_by_name("admins")
        group_manager.grant_repo_access("exists", admins.id, "admin")
        response = test_client.request(
            "DELETE",
            f"/api/v1/groups/{admins.id}/repos",
            json={"repos": ["exists", "does-not-exist"]},
        )
        assert response.status_code == 200
        assert response.json()["removed"] == 1
        repos = group_manager.get_group_repos(admins.id)
        assert "exists" not in repos
