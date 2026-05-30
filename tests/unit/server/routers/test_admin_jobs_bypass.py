"""
Integration tests for Story #1032 AC11: REST /api/jobs and /api/jobs/{job_id}
pass is_admin flag based on current_user.role.

Tests use _patch_closure to replace the background_job_manager closure in the
route handlers, then assert the mock was called with the expected is_admin value.

Covered scenarios:
1. GET /api/jobs as admin -> list_jobs called with is_admin=True
2. GET /api/jobs as non-admin -> list_jobs called with is_admin=False
3. GET /api/jobs/{job_id} as admin -> get_job_status called with is_admin=True
4. GET /api/jobs/{job_id} as non-admin -> get_job_status called with is_admin=False
5. Admin can see a job that belongs to another user via GET /api/jobs
6. Non-admin cannot see another user's job via GET /api/jobs
"""

import pytest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import (
    get_current_user,
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user_hybrid,
)

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    _make_admin,
    _make_regular_user,
    _find_elevation_check_dependencies,
    admin_client,  # noqa: F401, F811
    user_client,  # noqa: F401, F811
)


@pytest.fixture
def admin_client_hybrid():
    """TestClient with admin user overriding BOTH standard and hybrid auth."""
    admin = _make_admin()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
    app.dependency_overrides[get_current_user_hybrid] = lambda: admin
    for check_dep in _find_elevation_check_dependencies():
        app.dependency_overrides[check_dep] = lambda: admin
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def user_client_hybrid():
    """TestClient with regular user overriding BOTH standard and hybrid auth."""
    user = _make_regular_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper: build a minimal job dict that satisfies JobStatusResponse
# ---------------------------------------------------------------------------


def _make_job_dict(job_id: str, username: str) -> dict:
    return {
        "job_id": job_id,
        "operation_type": "deactivate_repository",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": None,
        "completed_at": None,
        "progress": 100,
        "result": None,
        "error": None,
        "username": username,
        "current_phase": None,
        "phase_detail": None,
    }


# ---------------------------------------------------------------------------
# GET /api/jobs — is_admin flag
# ---------------------------------------------------------------------------


class TestListJobsAdminFlagPropagation:
    """GET /api/jobs must pass is_admin=True for admin, False for non-admin."""

    def test_admin_receives_is_admin_true(self, admin_client):  # noqa: F811
        """Admin user's GET /api/jobs calls list_jobs with is_admin=True."""
        handler = _find_route_handler("/api/jobs", "GET")
        mock_bjm = Mock()
        mock_bjm.list_jobs.return_value = {
            "jobs": [],
            "total": 0,
            "limit": 10,
            "offset": 0,
        }

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = admin_client.get("/api/jobs")

        assert response.status_code == 200
        assert mock_bjm.list_jobs.called
        _, kwargs = mock_bjm.list_jobs.call_args
        assert kwargs.get("is_admin") is True, (
            "Admin user must invoke list_jobs with is_admin=True"
        )

    def test_non_admin_receives_is_admin_false(self, user_client):  # noqa: F811
        """Non-admin user's GET /api/jobs calls list_jobs with is_admin=False."""
        handler = _find_route_handler("/api/jobs", "GET")
        mock_bjm = Mock()
        mock_bjm.list_jobs.return_value = {
            "jobs": [],
            "total": 0,
            "limit": 10,
            "offset": 0,
        }

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = user_client.get("/api/jobs")

        assert response.status_code == 200
        assert mock_bjm.list_jobs.called
        _, kwargs = mock_bjm.list_jobs.call_args
        assert kwargs.get("is_admin") is False, (
            "Non-admin user must invoke list_jobs with is_admin=False"
        )

    def test_admin_sees_other_users_jobs_in_response(self, admin_client):  # noqa: F811
        """Admin GET /api/jobs returns jobs owned by other users."""
        handler = _find_route_handler("/api/jobs", "GET")
        other_user_job = _make_job_dict("job-abc-123", "bob")
        mock_bjm = Mock()
        mock_bjm.list_jobs.return_value = {
            "jobs": [other_user_job],
            "total": 1,
            "limit": 10,
            "offset": 0,
        }

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = admin_client.get("/api/jobs")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["jobs"][0]["username"] == "bob"

    def test_non_admin_only_sees_own_jobs(self, user_client):  # noqa: F811
        """Non-admin GET /api/jobs — mock returns only own user's jobs."""
        handler = _find_route_handler("/api/jobs", "GET")
        own_job = _make_job_dict("job-own-456", "testuser")
        mock_bjm = Mock()
        mock_bjm.list_jobs.return_value = {
            "jobs": [own_job],
            "total": 1,
            "limit": 10,
            "offset": 0,
        }

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = user_client.get("/api/jobs")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["jobs"][0]["username"] == "testuser"


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id} — is_admin flag
# (Uses hybrid auth fixtures because endpoint uses get_current_user_hybrid)
# ---------------------------------------------------------------------------


class TestGetJobStatusAdminFlagPropagation:
    """GET /api/jobs/{job_id} must pass is_admin=True for admin, False for non-admin."""

    def test_admin_receives_is_admin_true(self, admin_client_hybrid):
        """Admin user's GET /api/jobs/{job_id} calls get_job_status with is_admin=True."""
        handler = _find_route_handler("/api/jobs/{job_id}", "GET")
        mock_bjm = Mock()
        mock_bjm.get_job_status.return_value = _make_job_dict("job-xyz", "bob")

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = admin_client_hybrid.get("/api/jobs/job-xyz")

        assert response.status_code == 200
        assert mock_bjm.get_job_status.called
        args, kwargs = mock_bjm.get_job_status.call_args
        # is_admin can be positional or keyword depending on implementation
        is_admin_val = kwargs.get("is_admin", args[2] if len(args) > 2 else None)
        assert is_admin_val is True, (
            "Admin user must invoke get_job_status with is_admin=True"
        )

    def test_non_admin_receives_is_admin_false(self, user_client_hybrid):
        """Non-admin GET /api/jobs/{job_id} calls get_job_status with is_admin=False."""
        handler = _find_route_handler("/api/jobs/{job_id}", "GET")
        mock_bjm = Mock()
        mock_bjm.get_job_status.return_value = _make_job_dict("job-own", "testuser")

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = user_client_hybrid.get("/api/jobs/job-own")

        assert response.status_code == 200
        assert mock_bjm.get_job_status.called
        args, kwargs = mock_bjm.get_job_status.call_args
        is_admin_val = kwargs.get("is_admin", args[2] if len(args) > 2 else None)
        assert is_admin_val is False, (
            "Non-admin user must invoke get_job_status with is_admin=False"
        )

    def test_admin_can_get_other_users_job_status(self, admin_client_hybrid):
        """Admin GET /api/jobs/{job_id} returns job owned by another user."""
        handler = _find_route_handler("/api/jobs/{job_id}", "GET")
        mock_bjm = Mock()
        mock_bjm.get_job_status.return_value = _make_job_dict("job-bob", "bob")

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = admin_client_hybrid.get("/api/jobs/job-bob")

        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "bob"

    def test_non_admin_gets_404_for_missing_job(self, user_client_hybrid):
        """Non-admin GET /api/jobs/{job_id} returns 404 when get_job_status returns None."""
        handler = _find_route_handler("/api/jobs/{job_id}", "GET")
        mock_bjm = Mock()
        mock_bjm.get_job_status.return_value = None

        with _patch_closure(handler, "background_job_manager", mock_bjm):
            response = user_client_hybrid.get("/api/jobs/nonexistent")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/repos — deactivation_job field (AC4)
# ---------------------------------------------------------------------------


class TestGetReposDeactivationJobField:
    """AC4: GET /api/repos includes deactivation_job per repo, populated when active."""

    def _make_repo_dict(self, user_alias: str) -> dict:
        return {
            "user_alias": user_alias,
            "golden_repo_alias": "my-golden",
            "current_branch": "main",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "last_accessed": "2026-01-01T00:00:00+00:00",
        }

    def test_deactivation_job_null_when_no_active_job(self, user_client):  # noqa: F811
        """GET /api/repos returns deactivation_job=null when no active deactivation."""
        handler = _find_route_handler("/api/repos", "GET")
        mock_arm = Mock()
        mock_arm.list_activated_repositories.return_value = [
            self._make_repo_dict("myrepo")
        ]
        mock_bjm = Mock()
        # No deactivate_repository jobs
        mock_bjm.list_jobs.return_value = {
            "jobs": [],
            "total": 0,
            "limit": 500,
            "offset": 0,
        }

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "background_job_manager", mock_bjm):
                response = user_client.get("/api/repos")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["repositories"][0]["deactivation_job"] is None

    def test_deactivation_job_populated_when_active(self, user_client):  # noqa: F811
        """GET /api/repos returns deactivation_job with job_id and status when running."""
        handler = _find_route_handler("/api/repos", "GET")
        mock_arm = Mock()
        mock_arm.list_activated_repositories.return_value = [
            self._make_repo_dict("myrepo")
        ]
        mock_bjm = Mock()
        mock_bjm.list_jobs.return_value = {
            "jobs": [
                {
                    "job_id": "deact-job-abc",
                    "operation_type": "deactivate_repository",
                    "status": "running",
                    "repo_alias": "myrepo",
                    "username": "testuser",
                }
            ],
            "total": 1,
            "limit": 500,
            "offset": 0,
        }

        with _patch_closure(handler, "activated_repo_manager", mock_arm):
            with _patch_closure(handler, "background_job_manager", mock_bjm):
                response = user_client.get("/api/repos")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        repo = data["repositories"][0]
        assert repo["deactivation_job"] is not None
        assert repo["deactivation_job"]["job_id"] == "deact-job-abc"
        assert repo["deactivation_job"]["status"] == "running"
