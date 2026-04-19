"""
Unit tests for GET /api/repos/{user_alias}/sync-status endpoint.

Bug #824 — missing sync-status handler.

Tests:
  test_sync_status_happy_path                 -- 200 with full response shape validation
  test_sync_status_defaults_when_no_status    -- sync_status defaults to "synced"
  test_sync_status_with_conflicts             -- has_conflicts=True when conflict_details set
  test_sync_status_unknown_alias_returns_404  -- 404 when alias not activated
  test_sync_status_no_auth_returns_401_or_403 -- 401 or 403 without auth (isolation ensured)
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
)

# Expected keys in the sync-status response
_EXPECTED_KEYS = {"current_branch", "sync_status", "last_sync_time", "has_conflicts"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_user():
    user = Mock()
    user.username = "testuser"
    return user


@pytest.fixture()
def test_client(mock_user):
    """Function-scoped client with guaranteed override cleanup via try/finally."""

    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@contextmanager
def arm_mock(metadata):
    """Patch activated_repo_manager closure in the sync-status handler."""
    handler = _find_route_handler("/api/repos/{user_alias}/sync-status", "GET")
    mock_arm = Mock()
    mock_arm._load_metadata.return_value = metadata
    with _patch_closure(handler, "activated_repo_manager", mock_arm):
        yield mock_arm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sync_status_happy_path(test_client):
    """GET /api/repos/{alias}/sync-status returns 200 with all required keys."""
    metadata = {
        "user_alias": "myrepo",
        "current_branch": "main",
        "last_accessed": "2026-01-01T10:00:00",
        "sync_status": "ahead",
        "conflict_details": None,
    }
    with arm_mock(metadata):
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 200
    data = response.json()
    # Full shape validation: exactly the expected keys
    assert set(data.keys()) == _EXPECTED_KEYS, (
        f"Response keys {set(data.keys())} differ from expected {_EXPECTED_KEYS}"
    )
    assert data["current_branch"] == "main"
    assert data["sync_status"] == "ahead"
    assert data["has_conflicts"] is False
    assert data["last_sync_time"] == "2026-01-01T10:00:00"


def test_sync_status_defaults_when_no_status(test_client):
    """GET sync-status returns sync_status='synced' when metadata lacks sync_status key."""
    metadata = {
        "user_alias": "myrepo",
        "current_branch": "develop",
        "last_accessed": "2026-01-01T12:00:00",
        # no sync_status key at all — handler must default to "synced"
    }
    with arm_mock(metadata):
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == _EXPECTED_KEYS
    assert data["sync_status"] == "synced"
    assert data["current_branch"] == "develop"
    assert data["has_conflicts"] is False


def test_sync_status_with_conflicts(test_client):
    """GET sync-status returns has_conflicts=True when conflict_details is non-empty."""
    metadata = {
        "user_alias": "myrepo",
        "current_branch": "feature",
        "last_accessed": "2026-01-01T09:00:00",
        "sync_status": "diverged",
        "conflict_details": "conflict in file.txt",
    }
    with arm_mock(metadata):
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == _EXPECTED_KEYS
    assert data["sync_status"] == "diverged"
    assert data["has_conflicts"] is True


def test_sync_status_unknown_alias_returns_404(test_client):
    """GET /api/repos/{alias}/sync-status returns 404 when alias not activated."""
    with arm_mock(None):
        response = test_client.get("/api/repos/no_such_repo/sync-status")

    assert response.status_code == 404


def test_sync_status_no_auth_returns_401_or_403():
    """GET /api/repos/{alias}/sync-status without auth returns 401 or 403.

    The real authentication check must run, so dependency_overrides are cleared.
    Previous state is saved and restored in a finally block to guarantee isolation.
    FastAPI may return 401 (missing bearer) or 403 (forbidden) depending on scheme.
    """
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/repos/somerepo/sync-status")
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.update(previous_overrides)
