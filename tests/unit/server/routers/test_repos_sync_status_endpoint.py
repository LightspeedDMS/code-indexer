"""
Unit tests for repo sync-status routes (Bug #1740 / Bug #1743).

Covers:
- GET /api/repos/{user_alias}/sync-status  -- per-repo route, now backed by
  the REAL ActivatedRepoManager.compute_sync_status() instead of the old
  facade (metadata.get("sync_status") -- a key nothing ever wrote).
- GET /api/repos/sync-status  -- NEW bulk route (Bug #1743). Previously this
  path did not exist as a registered route at all; requests for it were
  captured by the pre-existing /api/repos/{user_alias} route (matching
  user_alias="sync-status"), producing a live 404 "Repository 'sync-status'
  not found or not activated". This route MUST be registered so it is not
  swallowed by any {user_alias}-shaped route.

Both handlers are closures over manager instances (registered via
register_repo_routes(app, *, activated_repo_manager=..., ...) in
inline_repos.py), not FastAPI Depends()-injected services -- so, matching
every sibling test module for this router (see
inline_routes_test_helpers.py, used across the router test suite), tests
here patch the closure cell directly via _patch_closure rather than
overriding a dependency that does not exist for these handlers.

Tests:
  test_sync_status_happy_path                  -- 200, full response shape
  test_sync_status_reflects_needs_sync          -- non-"synced" value flows through
  test_sync_status_with_conflicts               -- has_conflicts True + details
  test_sync_status_unknown_alias_returns_404    -- 404 when alias not activated
  test_sync_status_unexpected_error_returns_500_not_crash -- code-review
    finding #1: a non-ActivatedRepoError from compute_sync_status must
    produce a clean 500, not an unhandled exception escaping the route
  test_sync_status_no_auth_returns_401_or_403   -- 401/403 without auth

  test_bulk_sync_status_not_swallowed_by_user_alias_route -- Bug #1743 regression:
    a live TestClient GET to /api/repos/sync-status must NOT hit the 404 that
    /api/repos/{user_alias} would produce for alias "sync-status".
  test_bulk_sync_status_happy_path              -- 200, alias -> status mapping
  test_bulk_sync_status_empty_when_no_repos      -- {} when user has no activated repos
  test_bulk_sync_status_skips_repo_that_raises   -- a repo whose compute_sync_status
    raises ActivatedRepoError is omitted, not a 500
  test_bulk_sync_status_skips_repo_that_raises_unexpected_error -- same, for
    a non-ActivatedRepoError exception (code-review finding #1)
  test_bulk_sync_status_no_auth_returns_401_or_403
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
)
from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
)

# Expected keys in the sync-status response. conflict_details (finding #5)
# is wired through so the text compute_sync_status already produces isn't
# an orphan value discarded by every route that computes it.
_EXPECTED_KEYS = {
    "current_branch",
    "sync_status",
    "last_sync_time",
    "has_conflicts",
    "conflict_details",
}


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


def _assert_unauthenticated_request_rejected(path: str) -> None:
    """Shared no-auth assertion: a real (non-overridden) request to *path*
    must be rejected with 401 or 403. Saves/restores dependency_overrides
    around a fresh, override-free TestClient so this never leaks state to
    other tests regardless of call order.
    """
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(path)
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.update(previous_overrides)


def _compute_result(
    current_branch="main",
    sync_status="synced",
    has_conflicts=False,
    conflict_details=None,
    last_sync_time="2026-01-01T10:00:00",
):
    return {
        "current_branch": current_branch,
        "sync_status": sync_status,
        "has_conflicts": has_conflicts,
        "conflict_details": conflict_details,
        "last_sync_time": last_sync_time,
    }


@contextmanager
def per_repo_arm_mock(compute_result=None, compute_side_effect=None):
    """Patch activated_repo_manager closure in the per-repo sync-status handler."""
    handler = _find_route_handler("/api/repos/{user_alias}/sync-status", "GET")
    mock_arm = Mock()
    if compute_side_effect is not None:
        mock_arm.compute_sync_status.side_effect = compute_side_effect
    else:
        mock_arm.compute_sync_status.return_value = compute_result
    with _patch_closure(handler, "activated_repo_manager", mock_arm):
        yield mock_arm


@contextmanager
def bulk_arm_mock(repos, compute_side_effect=None, compute_results=None):
    """Patch activated_repo_manager closure in the bulk sync-status handler."""
    handler = _find_route_handler("/api/repos/sync-status", "GET")
    mock_arm = Mock()
    mock_arm.list_activated_repositories.return_value = repos
    if compute_side_effect is not None:
        mock_arm.compute_sync_status.side_effect = compute_side_effect
    elif compute_results is not None:
        mock_arm.compute_sync_status.side_effect = lambda username, alias: (
            compute_results[alias]
        )
    with _patch_closure(handler, "activated_repo_manager", mock_arm):
        yield mock_arm


# ---------------------------------------------------------------------------
# Per-repo route tests
# ---------------------------------------------------------------------------


def test_sync_status_happy_path(test_client):
    """GET /api/repos/{alias}/sync-status returns 200 with all required keys,
    sourced from ActivatedRepoManager.compute_sync_status (real computation).
    """
    with per_repo_arm_mock(
        _compute_result(current_branch="main", sync_status="synced")
    ) as mock_arm:
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == _EXPECTED_KEYS, (
        f"Response keys {set(data.keys())} differ from expected {_EXPECTED_KEYS}"
    )
    assert data["current_branch"] == "main"
    assert data["sync_status"] == "synced"
    assert data["has_conflicts"] is False
    assert data["conflict_details"] is None
    assert data["last_sync_time"] == "2026-01-01T10:00:00"
    mock_arm.compute_sync_status.assert_called_once_with("testuser", "myrepo")


def test_sync_status_reflects_needs_sync(test_client):
    """A real 'needs_sync' classification from compute_sync_status flows
    through the route untouched -- proves the handler no longer hardcodes
    or defaults the value."""
    with per_repo_arm_mock(
        _compute_result(current_branch="develop", sync_status="needs_sync")
    ):
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert data["sync_status"] == "needs_sync"
    assert data["current_branch"] == "develop"
    assert data["has_conflicts"] is False


def test_sync_status_with_conflicts(test_client):
    """has_conflicts=True, sync_status='conflict', and conflict_details
    all flow through the route's response contract."""
    with per_repo_arm_mock(
        _compute_result(
            current_branch="feature",
            sync_status="conflict",
            has_conflicts=True,
            conflict_details="Unmerged paths: file.txt",
        )
    ):
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == _EXPECTED_KEYS
    assert data["sync_status"] == "conflict"
    assert data["has_conflicts"] is True
    assert data["conflict_details"] == "Unmerged paths: file.txt"


def test_sync_status_unknown_alias_returns_404(test_client):
    """GET /api/repos/{alias}/sync-status returns 404 when compute_sync_status
    raises ActivatedRepoError (repo not found/not activated)."""
    with per_repo_arm_mock(
        compute_side_effect=ActivatedRepoError("Repository 'no_such_repo' not found")
    ):
        response = test_client.get("/api/repos/no_such_repo/sync-status")

    assert response.status_code == 404


def test_sync_status_unexpected_error_returns_500_not_crash(test_client):
    """Code-review finding #1: a non-ActivatedRepoError exception from
    compute_sync_status (e.g. an unexpected bug, or a misbehaving test
    double) must produce a clean, structured 500 response -- not an
    unhandled exception escaping the route (which, with this fixture's
    default raise_server_exceptions=True TestClient, would propagate as a
    raw Python exception out of client.get() instead of returning a
    Response at all).
    """
    with per_repo_arm_mock(compute_side_effect=RuntimeError("boom")):
        response = test_client.get("/api/repos/myrepo/sync-status")

    assert response.status_code == 500


def test_sync_status_no_auth_returns_401_or_403():
    """GET /api/repos/{alias}/sync-status without auth returns 401 or 403."""
    _assert_unauthenticated_request_rejected("/api/repos/somerepo/sync-status")


# ---------------------------------------------------------------------------
# Bulk route tests (Bug #1743)
# ---------------------------------------------------------------------------


def test_bulk_sync_status_not_swallowed_by_user_alias_route(test_client):
    """Regression test for Bug #1743: a live request to GET
    /api/repos/sync-status must be handled by the dedicated bulk route, NOT
    by GET /api/repos/{user_alias} matching user_alias='sync-status' (which
    previously produced a 404 'Repository sync-status not found or not
    activated'). This is verified via a real TestClient HTTP request through
    the full ASGI routing layer, not just decorator-order inspection.
    """
    with bulk_arm_mock(repos=[]):
        response = test_client.get("/api/repos/sync-status")

    assert response.status_code == 200, (
        "GET /api/repos/sync-status was swallowed by /api/repos/{user_alias} "
        f"(got {response.status_code}: {response.text})"
    )
    # The old collision produced this exact 404 detail; assert it is gone.
    assert "not found or not activated" not in response.text


def test_bulk_sync_status_happy_path(test_client):
    """GET /api/repos/sync-status returns alias -> status mapping for all of
    the current user's activated repositories, reusing compute_sync_status
    (no duplicated logic)."""
    repos = [
        {"user_alias": "web-app"},
        {"user_alias": "api-service"},
    ]
    results = {
        "web-app": _compute_result(current_branch="main", sync_status="synced"),
        "api-service": _compute_result(
            current_branch="develop", sync_status="needs_sync"
        ),
    }
    with bulk_arm_mock(repos=repos, compute_results=results) as mock_arm:
        response = test_client.get("/api/repos/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"web-app", "api-service"}
    assert set(data["web-app"].keys()) == _EXPECTED_KEYS
    assert data["web-app"]["sync_status"] == "synced"
    assert data["web-app"]["conflict_details"] is None
    assert data["api-service"]["sync_status"] == "needs_sync"
    assert mock_arm.compute_sync_status.call_count == 2


def test_bulk_sync_status_empty_when_no_repos(test_client):
    """GET /api/repos/sync-status returns {} when the user has no activated
    repositories -- not a 404 or 500."""
    with bulk_arm_mock(repos=[]):
        response = test_client.get("/api/repos/sync-status")

    assert response.status_code == 200
    assert response.json() == {}


def test_bulk_sync_status_skips_repo_that_raises(test_client):
    """A repo whose compute_sync_status raises ActivatedRepoError (e.g. a
    race with deactivation) is omitted from the result rather than failing
    the whole bulk request with a 500."""
    repos = [{"user_alias": "web-app"}, {"user_alias": "vanished"}]

    def side_effect(username, alias):
        if alias == "vanished":
            raise ActivatedRepoError("Repository 'vanished' not found")
        return _compute_result(sync_status="synced")

    with bulk_arm_mock(repos=repos, compute_side_effect=side_effect):
        response = test_client.get("/api/repos/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"web-app"}


def test_bulk_sync_status_skips_repo_that_raises_unexpected_error(test_client):
    """Code-review finding #1: a non-ActivatedRepoError exception from one
    repo's compute_sync_status must not crash the whole bulk request --
    that repo is omitted and the rest of the mapping is still returned.
    """
    repos = [{"user_alias": "web-app"}, {"user_alias": "broken"}]

    def side_effect(username, alias):
        if alias == "broken":
            raise RuntimeError("boom")
        return _compute_result(sync_status="synced")

    with bulk_arm_mock(repos=repos, compute_side_effect=side_effect):
        response = test_client.get("/api/repos/sync-status")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"web-app"}


def test_bulk_sync_status_no_auth_returns_401_or_403():
    """GET /api/repos/sync-status without auth returns 401 or 403."""
    _assert_unauthenticated_request_rejected("/api/repos/sync-status")
