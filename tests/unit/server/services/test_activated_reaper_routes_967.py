"""
Unit tests for Story #967: POST /api/admin/reaper/trigger endpoint.

TDD: Tests written BEFORE implementation. All should fail (red phase) until
the trigger endpoint is added to admin_api.py.

Acceptance Criteria covered:
  AC3 - Cycle visible as background job via trigger endpoint
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routes.reaper_routes import router


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_user(username: str, role: UserRole) -> User:
    """Build a User instance with required fields."""
    return User(
        username=username,
        role=role,
        email=None,
        password_hash="hashed",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def admin_user():
    return _make_user("admin", UserRole.ADMIN)


@pytest.fixture
def non_admin_user():
    return _make_user("regular", UserRole.NORMAL_USER)


@pytest.fixture
def app_with_scheduler(admin_user):
    """FastAPI app with admin_api router and a mock scheduler in app.state."""
    app = FastAPI()
    app.include_router(router, prefix="/api/admin")

    mock_scheduler = MagicMock()
    mock_scheduler.trigger_now.return_value = "job-abc123"
    app.state.activated_reaper_scheduler = mock_scheduler

    return app, mock_scheduler


@pytest.fixture
def app_without_scheduler():
    """FastAPI app with admin_api router but NO scheduler in app.state."""
    app = FastAPI()
    app.include_router(router, prefix="/api/admin")
    return app


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestReaperTriggerAuthorization:
    """POST /reaper/trigger enforces admin role."""

    def test_non_admin_gets_403(self, app_with_scheduler, non_admin_user):
        app, _ = app_with_scheduler
        app.dependency_overrides[get_current_user] = lambda: non_admin_user
        try:
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/api/admin/reaper/trigger")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403

    def test_admin_gets_200(self, app_with_scheduler, admin_user):
        app, _ = app_with_scheduler
        app.dependency_overrides[get_current_user] = lambda: admin_user
        try:
            client = TestClient(app)
            response = client.post("/api/admin/reaper/trigger")
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Response shape tests
# ---------------------------------------------------------------------------


class TestReaperTriggerResponse:
    """POST /reaper/trigger returns correct response shape."""

    def test_response_contains_job_id(self, app_with_scheduler, admin_user):
        """Admin trigger response includes job_id field."""
        app, _ = app_with_scheduler
        app.dependency_overrides[get_current_user] = lambda: admin_user
        try:
            client = TestClient(app)
            response = client.post("/api/admin/reaper/trigger")
        finally:
            app.dependency_overrides.clear()

        data = response.json()
        assert "job_id" in data
        assert data["job_id"] == "job-abc123"

    def test_response_contains_status_submitted(self, app_with_scheduler, admin_user):
        """Admin trigger response includes status='submitted'."""
        app, _ = app_with_scheduler
        app.dependency_overrides[get_current_user] = lambda: admin_user
        try:
            client = TestClient(app)
            response = client.post("/api/admin/reaper/trigger")
        finally:
            app.dependency_overrides.clear()

        data = response.json()
        assert data["status"] == "submitted"

    def test_trigger_calls_scheduler_trigger_now(self, app_with_scheduler, admin_user):
        """POST /reaper/trigger delegates to scheduler.trigger_now()."""
        app, mock_scheduler = app_with_scheduler
        app.dependency_overrides[get_current_user] = lambda: admin_user
        try:
            client = TestClient(app)
            client.post("/api/admin/reaper/trigger")
        finally:
            app.dependency_overrides.clear()

        mock_scheduler.trigger_now.assert_called_once()


# ---------------------------------------------------------------------------
# Scheduler not running
# ---------------------------------------------------------------------------


class TestReaperTriggerSchedulerAbsent:
    """POST /reaper/trigger returns 503 when scheduler not in app.state."""

    def test_returns_503_when_scheduler_not_running(
        self, app_without_scheduler, admin_user
    ):
        """503 returned if activated_reaper_scheduler absent from app.state."""
        app_without_scheduler.dependency_overrides[get_current_user] = (
            lambda: admin_user
        )
        try:
            client = TestClient(app_without_scheduler, raise_server_exceptions=False)
            response = client.post("/api/admin/reaper/trigger")
        finally:
            app_without_scheduler.dependency_overrides.clear()

        assert response.status_code == 503
