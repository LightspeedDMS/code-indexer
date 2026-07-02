# ruff: noqa: F811
"""
Route-level coverage tests for stats, status summary, and favicon inline routes.

Covers:
9.  GET /api/repositories/{repo_id}/stats
10. GET /api/repos/status
11. GET /favicon.ico
"""

from unittest.mock import Mock, patch

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    user_client,  # noqa: F401
    anon_client,  # noqa: F401
)


# ---------------------------------------------------------------------------
# 9. GET /api/repositories/{repo_id}/stats
# ---------------------------------------------------------------------------


class TestGetRepositoryStats:
    """GET /api/repositories/{repo_id}/stats"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/repositories/{repo_id}/stats", "GET")
        assert handler is not None

    def test_requires_auth(self, anon_client):
        response = anon_client.get("/api/repositories/myrepo/stats")
        assert response.status_code == 401

    def test_repo_not_found_returns_404(self, user_client):
        with patch(
            "code_indexer.server.services.stats_service"
            ".RepositoryStatsService.get_repository_stats",
            side_effect=FileNotFoundError("Repo not found"),
        ):
            response = user_client.get("/api/repositories/nonexistent/stats")

        assert response.status_code == 404

    def test_permission_denied_returns_403(self, user_client):
        with patch(
            "code_indexer.server.services.stats_service"
            ".RepositoryStatsService.get_repository_stats",
            side_effect=PermissionError("Access denied"),
        ):
            response = user_client.get("/api/repositories/locked-repo/stats")

        assert response.status_code == 403

    def test_success_returns_200(self, user_client):
        from datetime import datetime, timezone
        from code_indexer.server.models.api_models import (
            RepositoryStatsResponse,
            RepositoryFilesInfo,
            RepositoryStorageInfo,
            RepositoryActivityInfo,
            RepositoryHealthInfo,
        )

        real_stats = RepositoryStatsResponse(
            repository_id="myrepo",
            files=RepositoryFilesInfo(total=10, indexed=10, by_language={"python": 10}),
            storage=RepositoryStorageInfo(
                repository_size_bytes=1024,
                index_size_bytes=512,
                embedding_count=10,
            ),
            activity=RepositoryActivityInfo(
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                last_sync_at=None,
                last_accessed_at=None,
                sync_count=0,
            ),
            health=RepositoryHealthInfo(score=1.0, issues=[]),
        )

        with patch(
            "code_indexer.server.services.stats_service"
            ".RepositoryStatsService.get_repository_stats",
            return_value=real_stats,
        ):
            response = user_client.get("/api/repositories/myrepo/stats")

        assert response.status_code == 200
        assert response.json()["repository_id"] == "myrepo"


# ---------------------------------------------------------------------------
# 10. GET /api/repos/status
# ---------------------------------------------------------------------------


class TestGetReposStatusSummary:
    """GET /api/repos/status"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/repos/status", "GET")
        assert handler is not None

    def test_requires_auth(self, anon_client):
        response = anon_client.get("/api/repos/status")
        assert response.status_code == 401

    def test_success_with_no_repos(self, user_client):
        handler = _find_route_handler("/api/repos/status", "GET")

        mock_arm = Mock()
        mock_arm.list_activated_repositories.return_value = []

        mock_grm = Mock()
        mock_grm.list_golden_repos.return_value = []

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "golden_repo_manager", mock_grm):
                response = user_client.get("/api/repos/status")

        assert response.status_code == 200
        data = response.json()
        assert "activated_repositories" in data
        assert data["activated_repositories"]["total_count"] == 0
        assert "available_repositories" in data
        assert "recommendations" in data
        # With no repos, expect an "activate" recommendation
        assert any("activate" in r.lower() for r in data["recommendations"])

    def test_success_counts_repo_sync_status(self, user_client):
        handler = _find_route_handler("/api/repos/status", "GET")

        mock_arm = Mock()
        mock_arm.list_activated_repositories.return_value = [
            {
                "user_alias": "backend-repo",
                "sync_status": "synced",
                "activated_at": "2025-01-15T10:00:00Z",
                "last_accessed": "2025-01-20T10:00:00Z",
            },
            {
                "user_alias": "frontend-repo",
                "sync_status": "needs_sync",
                "activated_at": "2025-01-10T10:00:00Z",
                "last_accessed": "2025-01-18T10:00:00Z",
            },
        ]

        mock_grm = Mock()
        mock_grm.list_golden_repos.return_value = [
            {"alias": "backend-repo"},
            {"alias": "frontend-repo"},
            {"alias": "infra-repo"},
        ]

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "golden_repo_manager", mock_grm):
                response = user_client.get("/api/repos/status")

        assert response.status_code == 200
        data = response.json()
        assert data["activated_repositories"]["total_count"] == 2
        assert data["activated_repositories"]["synced_count"] == 1
        assert data["activated_repositories"]["needs_sync_count"] == 1
        assert data["available_repositories"]["total_count"] == 3

    def test_error_returns_500(self, user_client):
        handler = _find_route_handler("/api/repos/status", "GET")

        mock_arm = Mock()
        mock_arm.list_activated_repositories.side_effect = RuntimeError(
            "Database error"
        )

        mock_grm = Mock()

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "golden_repo_manager", mock_grm):
                response = user_client.get("/api/repos/status")

        assert response.status_code == 500


# ---------------------------------------------------------------------------
# 11. GET /favicon.ico
# ---------------------------------------------------------------------------


class TestFaviconRoute:
    """GET /favicon.ico"""

    def test_route_registered(self):
        handler = _find_route_handler("/favicon.ico", "GET")
        assert handler is not None

    def test_favicon_redirects_without_auth(self, anon_client):
        """Favicon must be accessible without authentication."""
        response = anon_client.get("/favicon.ico", follow_redirects=False)
        assert response.status_code in (301, 302)

    def test_favicon_redirect_target_contains_favicon(self, anon_client):
        """The redirect location must point at the SVG favicon."""
        response = anon_client.get("/favicon.ico", follow_redirects=False)
        location = response.headers.get("location", "")
        assert "favicon" in location
