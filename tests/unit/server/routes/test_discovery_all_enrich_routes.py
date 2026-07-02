"""
Tests for new discovery all/enrich routes and deletion of old routes (Story #754).

RED phase: routes do not exist yet so new-route tests will fail;
old-route 404 tests will pass initially and must continue passing after deletion.

New routes:
  GET  /admin/api/discovery/{platform}/all    — requires admin auth, returns JSON
  POST /admin/api/discovery/{platform}/enrich — requires admin auth, accepts JSON body

Deleted routes (must return 404):
  GET  /admin/partials/auto-discovery/gitlab
  GET  /admin/partials/auto-discovery/github
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixture (pytest standard naming convention)
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """TestClient wrapping the web_router mounted at /admin."""
    from code_indexer.server.web.routes import web_router
    from unittest.mock import MagicMock

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")

    # Mock get_session_manager at the auth module level so that
    # _hybrid_auth_impl (which does a local import) doesn't crash.
    # Returns a SM with no active session → auth fails → 401.
    mock_sm = MagicMock()
    mock_sm.get_session.return_value = None

    with patch(
        "code_indexer.server.web.auth.get_session_manager", return_value=mock_sm
    ):
        yield TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# New endpoint existence: GET /admin/api/discovery/{platform}/all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_discovery_all_endpoint_exists(client, platform):
    """GET /admin/api/discovery/{platform}/all must exist (not 404)."""
    response = client.get(
        f"/admin/api/discovery/{platform}/all",
        follow_redirects=False,
    )
    assert response.status_code != 404, (
        f"Expected /admin/api/discovery/{platform}/all to exist, got 404"
    )


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_discovery_all_requires_auth(client, platform):
    """GET /admin/api/discovery/{platform}/all without session must require auth."""
    response = client.get(
        f"/admin/api/discovery/{platform}/all",
        follow_redirects=False,
    )
    assert response.status_code in (401, 302, 303, 307), (
        f"Expected auth redirect/401 for /all without auth, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# New endpoint existence: POST /admin/api/discovery/{platform}/enrich
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_discovery_enrich_endpoint_exists(client, platform):
    """POST /admin/api/discovery/{platform}/enrich must exist (not 404)."""
    response = client.post(
        f"/admin/api/discovery/{platform}/enrich",
        json={"clone_urls": ["https://example.com/repo.git"]},
        follow_redirects=False,
    )
    assert response.status_code != 404, (
        f"Expected /admin/api/discovery/{platform}/enrich to exist, got 404"
    )


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_discovery_enrich_requires_auth(client, platform):
    """POST /admin/api/discovery/{platform}/enrich without session must require auth."""
    response = client.post(
        f"/admin/api/discovery/{platform}/enrich",
        json={"clone_urls": ["https://example.com/repo.git"]},
        follow_redirects=False,
    )
    assert response.status_code in (401, 302, 303, 307), (
        f"Expected auth redirect/401 for /enrich without auth, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Validation: max 50 clone_urls per enrich request (Story #754)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_enrich_rejects_more_than_50_urls(platform):
    """POST /admin/api/discovery/{platform}/enrich with 51 clone_urls must return 400.

    Auth is bypassed in two layers so the request reaches body validation:
    - dependency_overrides[get_current_admin_user_hybrid]: bypasses _hybrid_auth_impl
    - patch _resolve_provider: bypasses the inline session check inside the route body
    """
    from code_indexer.server.web.routes import web_router
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")

    mock_user = MagicMock()
    mock_user.username = "admin"
    mock_user.has_permission.return_value = True
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: mock_user

    mock_provider = MagicMock()
    mock_provider.is_configured.return_value = True

    client = TestClient(app, raise_server_exceptions=False)

    too_many_urls = [f"https://example.com/repo{i}.git" for i in range(51)]

    with patch(
        "code_indexer.server.web.routes._resolve_provider",
        return_value=(mock_provider, None),
    ):
        response = client.post(
            f"/admin/api/discovery/{platform}/enrich",
            json={"clone_urls": too_many_urls},
            follow_redirects=False,
        )

    assert response.status_code == 400, (
        f"Expected 400 for 51 clone_urls, got {response.status_code}"
    )
    body = response.json()
    assert "50" in body.get("error", ""), (
        f"Expected error message to mention the limit (50), got: {body}"
    )


# ---------------------------------------------------------------------------
# Legacy partial routes are restored for backwards compat — require auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_legacy_partial_discovery_route_requires_authentication(client, platform):
    """GET /admin/partials/auto-discovery/{platform} must exist and return 401 without a session.

    These routes were restored for backwards compatibility after Story #754.
    An unauthenticated request must be rejected with 401, not 404.
    """
    response = client.get(
        f"/admin/partials/auto-discovery/{platform}",
        follow_redirects=False,
    )
    assert response.status_code == 401, (
        f"Expected GET /admin/partials/auto-discovery/{platform} to return 401 "
        f"(route exists but requires auth), got {response.status_code}"
    )
