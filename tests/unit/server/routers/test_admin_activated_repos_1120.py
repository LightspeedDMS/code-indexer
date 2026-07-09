"""
Tests for Bug #1120 admin cleanup routes:
  GET  /api/admin/activated-repos
  DELETE /api/admin/activated-repos/{username}/{user_alias}

Scenarios covered:
1. Admin GET returns repos across users (200).
2. Admin DELETE for a normal repo returns 202 with job_id.
3. Admin DELETE for a dangling repo (registry row present, on-disk dir missing) -> 202.
4. Non-admin on either route -> 403.
5. Admin DELETE for a genuinely nonexistent (username, alias) -> 404.
"""

from unittest.mock import Mock, MagicMock


from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoError

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    admin_client,  # noqa: F401
    user_client,  # noqa: F401
)


# ---------------------------------------------------------------------------
# GET /api/admin/activated-repos
# ---------------------------------------------------------------------------


class TestAdminListAllActivatedRepos:
    """GET /api/admin/activated-repos returns all repos across all users."""

    def test_admin_200_returns_repos(self, admin_client):  # noqa: F811
        handler = _find_route_handler("/api/admin/activated-repos", "GET")

        fake_repos = [
            {"username": "alice", "user_alias": "myrepo", "golden_repo_alias": "core"},
            {"username": "bob", "user_alias": "otherrepo", "golden_repo_alias": "core"},
        ]
        mock_arm = Mock()
        mock_arm.list_all_activated_repositories.return_value = fake_repos

        mock_grm = MagicMock()
        mock_grm.activated_repo_manager = mock_arm

        with _patch_closure(handler, "golden_repo_manager", mock_grm):
            response = admin_client.get("/api/admin/activated-repos")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["activated_repositories"]) == 2
        usernames = {r["username"] for r in data["activated_repositories"]}
        assert usernames == {"alice", "bob"}

    def test_non_admin_403(self, user_client):  # noqa: F811
        response = user_client.get("/api/admin/activated-repos")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/admin/activated-repos/{username}/{user_alias}
# ---------------------------------------------------------------------------


class TestAdminDeactivateActivatedRepo:
    """DELETE /api/admin/activated-repos/{username}/{user_alias}"""

    def _make_mock_grm(self, arm: Mock) -> MagicMock:
        mock_grm = MagicMock()
        mock_grm.activated_repo_manager = arm
        return mock_grm

    def test_admin_normal_repo_202_with_job_id(self, admin_client):  # noqa: F811
        handler = _find_route_handler(
            "/api/admin/activated-repos/{username}/{user_alias}", "DELETE"
        )
        mock_arm = Mock()
        mock_arm.deactivate_repository.return_value = "job-abc-123"

        with _patch_closure(
            handler, "golden_repo_manager", self._make_mock_grm(mock_arm)
        ):
            response = admin_client.delete("/api/admin/activated-repos/alice/myrepo")

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-abc-123"
        mock_arm.deactivate_repository.assert_called_once_with(
            username="alice",
            user_alias="myrepo",
            actor_username="testadmin",
        )

    def test_admin_dangling_repo_202(self, admin_client):  # noqa: F811
        """Registry row present but on-disk dir missing — must still return 202."""
        handler = _find_route_handler(
            "/api/admin/activated-repos/{username}/{user_alias}", "DELETE"
        )
        mock_arm = Mock()
        # deactivate_repository succeeds even for dangling (Bug #1120 fix)
        mock_arm.deactivate_repository.return_value = "job-dangling-456"

        with _patch_closure(
            handler, "golden_repo_manager", self._make_mock_grm(mock_arm)
        ):
            response = admin_client.delete(
                "/api/admin/activated-repos/alice/dangling-repo"
            )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-dangling-456"

    def test_admin_nonexistent_repo_404(self, admin_client):  # noqa: F811
        """Truly nonexistent (username, alias) -> ActivatedRepoError -> 404."""
        handler = _find_route_handler(
            "/api/admin/activated-repos/{username}/{user_alias}", "DELETE"
        )
        mock_arm = Mock()
        mock_arm.deactivate_repository.side_effect = ActivatedRepoError(
            "Repository 'ghost-repo' not found for user 'nobody'"
        )

        with _patch_closure(
            handler, "golden_repo_manager", self._make_mock_grm(mock_arm)
        ):
            response = admin_client.delete(
                "/api/admin/activated-repos/nobody/ghost-repo"
            )

        assert response.status_code == 404
        assert "ghost-repo" in response.json()["detail"]

    def test_non_admin_get_403(self, user_client):  # noqa: F811
        response = user_client.delete("/api/admin/activated-repos/alice/myrepo")
        assert response.status_code == 403
