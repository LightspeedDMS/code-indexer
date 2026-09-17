"""Regression test for Bug #1895: the admin Embedding Stats dashboard
(/admin/embedding-stats) rendered with NO top navigation bar because its
route's TemplateResponse context omitted `show_nav` (base.html gates the
`<nav class="admin-nav">` block behind `{% if show_nav %}`, and highlights
the active item via `current_page`).

Real-auth TestClient integration test (no mocking of the auth boundary),
mirroring tests/unit/server/web/test_backfill_journal_routes_1062.py: a real
login flow through /login establishes a genuine admin session, then the real
route is hit through the FastAPI app so the actual Jinja2Templates instance
renders the actual base.html/embedding_stats_dashboard.html templates.
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

# Credentials from env — the server seeds "admin"/"admin" as its test-only
# default (CLAUDE.md). Override via CIDX_TEST_ADMIN_USER / CIDX_TEST_ADMIN_PASSWORD.
_ADMIN_USERNAME = os.environ.get("CIDX_TEST_ADMIN_USER", "admin")
_ADMIN_PASSWORD = os.environ.get("CIDX_TEST_ADMIN_PASSWORD", "admin")


@pytest.fixture
def app():
    """Create FastAPI app with minimal startup."""
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie via form-based login."""
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    login_resp = client.post(
        "/login",
        data={
            "username": _ADMIN_USERNAME,
            "password": _ADMIN_PASSWORD,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies, "No session cookie set by form login"

    for name, value in login_resp.cookies.items():
        client.cookies.set(name, value)
    return login_resp.cookies


class TestEmbeddingStatsPageHasAdminNav:
    def test_embedding_stats_page_renders_admin_nav(
        self, client, admin_session_cookie
    ) -> None:
        r = client.get("/admin/embedding-stats")
        assert r.status_code == 200
        assert '<nav class="admin-nav">' in r.text, (
            "Expected the admin nav bar to be present on /admin/embedding-stats "
            f"(Bug #1895):\n{r.text[:800]}"
        )

    def test_embedding_stats_nav_item_highlighted(
        self, client, admin_session_cookie
    ) -> None:
        r = client.get("/admin/embedding-stats")
        assert r.status_code == 200
        assert 'aria-current="page"' in r.text
        assert re.search(r'/admin/embedding-stats"[^>]*aria-current="page"', r.text), (
            "Expected the 'Embedding Stats' nav item to be marked "
            f"aria-current='page':\n{r.text[:800]}"
        )
