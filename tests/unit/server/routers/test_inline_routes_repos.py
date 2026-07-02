# ruff: noqa: F811
"""
Route-level coverage tests for repo-related inline routes.

Covers:
4.  POST /api/admin/golden-repos/{alias}/refresh
5.  GET  /api/repos/activation/{job_id}/progress
6.  PUT  /api/repos/{user_alias}/sync
7.  POST /api/repos/sync
8.  GET  /api/repos/{user_alias}/branches
"""

from unittest.mock import Mock, patch

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    admin_client,  # noqa: F401
    user_client,  # noqa: F401
    anon_client,  # noqa: F401
)


# ---------------------------------------------------------------------------
# 4. POST /api/admin/golden-repos/{alias}/refresh
# ---------------------------------------------------------------------------


class TestRefreshGoldenRepo:
    """POST /api/admin/golden-repos/{alias}/refresh"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/refresh", "POST")
        assert handler is not None

    def test_requires_admin_auth(self, anon_client):
        response = anon_client.post("/api/admin/golden-repos/myrepo/refresh")
        assert response.status_code == 401

    def test_repo_not_found_returns_404(self, admin_client):
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/refresh", "POST")
        mock_grm = Mock()
        mock_grm.golden_repos = {}  # empty - alias not registered

        with _patch_closure(handler, "golden_repo_manager", mock_grm):
            response = admin_client.post(
                "/api/admin/golden-repos/nonexistent-repo/refresh"
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_scheduler_unavailable_returns_503(self, admin_client):
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/refresh", "POST")
        mock_grm = Mock()
        mock_grm.golden_repos = {"myrepo": Mock()}

        mock_app = Mock()
        mock_app.state.global_lifecycle_manager = None

        with _patch_closure(handler, "golden_repo_manager", mock_grm):
            with _patch_closure(handler, "app", mock_app):
                response = admin_client.post("/api/admin/golden-repos/myrepo/refresh")

        assert response.status_code == 503

    def test_success_returns_job_id_and_message(self, admin_client):
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/refresh", "POST")
        mock_grm = Mock()
        mock_grm.golden_repos = {"myrepo": Mock()}

        mock_scheduler = Mock()
        mock_scheduler.trigger_refresh_for_repo.return_value = "job-refresh-123"

        mock_lifecycle = Mock()
        mock_lifecycle.refresh_scheduler = mock_scheduler

        mock_app = Mock()
        mock_app.state.global_lifecycle_manager = mock_lifecycle

        with _patch_closure(handler, "golden_repo_manager", mock_grm):
            with _patch_closure(handler, "app", mock_app):
                response = admin_client.post("/api/admin/golden-repos/myrepo/refresh")

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-refresh-123"
        assert "myrepo" in data["message"]


# ---------------------------------------------------------------------------
# 5. GET /api/repos/activation/{job_id}/progress
# ---------------------------------------------------------------------------


class TestGetActivationProgress:
    """GET /api/repos/activation/{job_id}/progress"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/repos/activation/{job_id}/progress", "GET")
        assert handler is not None

    def test_requires_auth(self, anon_client):
        response = anon_client.get("/api/repos/activation/job-123/progress")
        assert response.status_code == 401

    def test_job_not_found_returns_404(self, user_client):
        handler = _find_route_handler("/api/repos/activation/{job_id}/progress", "GET")
        mock_arm = Mock()
        mock_arm.background_job_manager.get_job_status.return_value = None

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.get("/api/repos/activation/nonexistent/progress")

        assert response.status_code == 404

    def test_success_returns_enhanced_status_with_step(self, user_client):
        handler = _find_route_handler("/api/repos/activation/{job_id}/progress", "GET")
        mock_arm = Mock()
        mock_arm.background_job_manager.get_job_status.return_value = {
            "job_id": "job-abc",
            "status": "running",
            "progress": 50,
            "created_at": "2025-01-01T00:00:00Z",
            "started_at": "2025-01-01T00:00:01Z",
            "completed_at": None,
            "operation_type": "activate_repository",
            "error": None,
            "result": None,
        }

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.get("/api/repos/activation/job-abc/progress")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-abc"
        assert data["status"] == "running"
        assert data["progress_percentage"] == 50
        assert "current_step" in data

    def test_pending_job_shows_queued_step(self, user_client):
        handler = _find_route_handler("/api/repos/activation/{job_id}/progress", "GET")
        mock_arm = Mock()
        mock_arm.background_job_manager.get_job_status.return_value = {
            "job_id": "job-pending",
            "status": "pending",
            "progress": 0,
            "created_at": "2025-01-01T00:00:00Z",
            "started_at": None,
            "completed_at": None,
            "operation_type": "activate_repository",
            "error": None,
            "result": None,
        }

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.get("/api/repos/activation/job-pending/progress")

        assert response.status_code == 200
        data = response.json()
        assert "queued" in data["current_step"].lower()


# ---------------------------------------------------------------------------
# 6. PUT /api/repos/{user_alias}/sync
# ---------------------------------------------------------------------------


class TestSyncUserRepository:
    """PUT /api/repos/{user_alias}/sync"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/repos/{user_alias}/sync", "PUT")
        assert handler is not None

    def test_requires_auth(self, anon_client):
        response = anon_client.put("/api/repos/myrepo/sync")
        assert response.status_code == 401

    def test_repo_not_found_returns_404(self, user_client):
        handler = _find_route_handler("/api/repos/{user_alias}/sync", "PUT")

        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoError,
        )

        mock_arm = Mock()
        mock_arm.get_activated_repo_path.side_effect = ActivatedRepoError(
            "Repo not found"
        )

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.put("/api/repos/nonexistent/sync")

        assert response.status_code == 404

    def test_success_returns_sync_result(self, user_client):
        handler = _find_route_handler("/api/repos/{user_alias}/sync", "PUT")

        mock_arm = Mock()
        mock_arm.get_activated_repo_path.return_value = "/path/to/repo"
        mock_arm.sync_with_golden_repository.return_value = {
            "message": "Sync completed successfully",
            "changes_applied": True,
            "files_changed": 3,
            "changed_files": ["a.py", "b.py", "c.py"],
        }

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with patch(
                "code_indexer.server.validators.composite_repo_validator"
                ".CompositeRepoValidator.check_operation"
            ):
                response = user_client.put("/api/repos/myrepo/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["changes_applied"] is True
        assert "sync" in data["message"].lower()


# ---------------------------------------------------------------------------
# 7. POST /api/repos/sync
# ---------------------------------------------------------------------------


class TestGeneralRepositorySync:
    """POST /api/repos/sync"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/repos/sync", "POST")
        assert handler is not None

    def test_requires_auth(self, anon_client):
        response = anon_client.post(
            "/api/repos/sync", json={"repository_alias": "myrepo"}
        )
        assert response.status_code == 401

    def test_empty_alias_returns_422(self, user_client):
        handler = _find_route_handler("/api/repos/sync", "POST")
        mock_arm = Mock()
        mock_bgm = Mock()
        mock_rlm = Mock()

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "background_job_manager", mock_bgm):
                with _patch_closure(handler, "repository_listing_manager", mock_rlm):
                    response = user_client.post(
                        "/api/repos/sync",
                        json={"repository_alias": "   "},
                    )

        assert response.status_code == 422

    def test_repo_not_found_returns_404(self, user_client):
        handler = _find_route_handler("/api/repos/sync", "POST")

        mock_arm = Mock()
        mock_arm.list_activated_repositories.return_value = []

        mock_bgm = Mock()
        mock_rlm = Mock()
        mock_rlm.get_repository_details.side_effect = Exception("not found")

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "background_job_manager", mock_bgm):
                with _patch_closure(handler, "repository_listing_manager", mock_rlm):
                    response = user_client.post(
                        "/api/repos/sync",
                        json={"repository_alias": "missing-repo"},
                    )

        assert response.status_code == 404

    def test_success_submits_sync_job(self, user_client):
        handler = _find_route_handler("/api/repos/sync", "POST")

        mock_arm = Mock()
        mock_arm.list_activated_repositories.return_value = [
            {"user_alias": "myrepo", "actual_repo_id": "myrepo"}
        ]

        mock_bgm = Mock()
        mock_bgm.get_jobs_by_operation_and_params.return_value = []
        mock_bgm.submit_job.return_value = "sync-job-999"

        mock_rlm = Mock()

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "background_job_manager", mock_bgm):
                with _patch_closure(handler, "repository_listing_manager", mock_rlm):
                    with patch(
                        "code_indexer.server.app_helpers._execute_repository_sync"
                    ):
                        response = user_client.post(
                            "/api/repos/sync",
                            json={
                                "repository_alias": "myrepo",
                                "force": False,
                            },
                        )

        assert response.status_code in (200, 202)


# ---------------------------------------------------------------------------
# 8. GET /api/repos/{user_alias}/branches
# ---------------------------------------------------------------------------


class TestListRepositoryBranches:
    """GET /api/repos/{user_alias}/branches"""

    def test_route_registered(self):
        handler = _find_route_handler("/api/repos/{user_alias}/branches", "GET")
        assert handler is not None

    def test_requires_auth(self, anon_client):
        response = anon_client.get("/api/repos/myrepo/branches")
        assert response.status_code == 401

    def test_repo_not_found_returns_404(self, user_client):
        handler = _find_route_handler("/api/repos/{user_alias}/branches", "GET")
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoError,
        )

        mock_arm = Mock()
        mock_arm.list_repository_branches.side_effect = ActivatedRepoError(
            "Repo not found"
        )

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.get("/api/repos/nonexistent/branches")

        assert response.status_code == 404

    def test_success_returns_branch_list(self, user_client):
        handler = _find_route_handler("/api/repos/{user_alias}/branches", "GET")
        mock_arm = Mock()
        mock_arm.list_repository_branches.return_value = {
            "branches": [
                {
                    "name": "main",
                    "type": "local",
                    "is_current": True,
                    "remote_ref": None,
                    "last_commit_hash": "abc123",
                    "last_commit_message": "Initial commit",
                    "last_commit_date": "2025-01-01T00:00:00Z",
                },
                {
                    "name": "develop",
                    "type": "local",
                    "is_current": False,
                    "remote_ref": None,
                    "last_commit_hash": "def456",
                    "last_commit_message": "Feature",
                    "last_commit_date": "2025-01-02T00:00:00Z",
                },
            ],
            "current_branch": "main",
            "total_branches": 2,
            "local_branches": 2,
            "remote_branches": 0,
        }

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.get("/api/repos/myrepo/branches")

        assert response.status_code == 200
        data = response.json()
        assert data["current_branch"] == "main"
        assert data["total_branches"] == 2
        branch_names = [b["name"] for b in data["branches"]]
        assert "main" in branch_names
        assert "develop" in branch_names

    def test_git_error_returns_400(self, user_client):
        handler = _find_route_handler("/api/repos/{user_alias}/branches", "GET")
        from code_indexer.server.repositories.golden_repo_manager import (
            GitOperationError,
        )

        mock_arm = Mock()
        mock_arm.list_repository_branches.side_effect = GitOperationError(
            "Git operation failed"
        )

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            response = user_client.get("/api/repos/myrepo/branches")

        assert response.status_code == 400
