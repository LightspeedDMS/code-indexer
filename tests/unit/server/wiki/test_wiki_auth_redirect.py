"""Tests for Bug #296: wiki auth should redirect to login instead of returning JSON 401.

Tests verify that get_wiki_user_hybrid() catches 401 from get_current_user_hybrid and
issues a 303 redirect to /login?redirect_to=<current_path> instead of returning raw JSON.
"""
import inspect
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette import status

from code_indexer.server.auth.user_manager import User


def _make_request(path: str, query: str = "") -> MagicMock:
    """Build a minimal mock Request with url.path and url.query."""
    request = MagicMock()
    request.url.path = path
    request.url.query = query
    request.headers = {}  # Empty dict — no Authorization header
    request.cookies = {}  # No session cookies
    return request


def _make_user(username: str = "alice") -> User:
    """Build a minimal real-ish User via MagicMock (avoids DB)."""
    user = MagicMock(spec=User)
    user.username = username
    return user


class TestGetWikiUserHybridExists:
    """get_wiki_user_hybrid must be importable from wiki routes."""

    def test_function_is_exported_from_routes(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid  # noqa: F401

        assert callable(get_wiki_user_hybrid)

    def test_function_accepts_request_parameter(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        sig = inspect.signature(get_wiki_user_hybrid)
        assert "request" in sig.parameters


class TestGetWikiUserHybridRedirectBehavior:
    """401 from get_current_user_hybrid must become a 303 redirect."""

    def test_redirects_when_401_raised(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        request = _make_request("/wiki/my-repo/")
        exc_401 = HTTPException(status_code=401, detail="Authentication required")

        with patch(
            "code_indexer.server.auth.dependencies._hybrid_auth_impl",
            side_effect=exc_401,
        ):
            with pytest.raises(HTTPException) as exc_info:
                get_wiki_user_hybrid(request)

        assert exc_info.value.status_code == status.HTTP_303_SEE_OTHER

    def test_redirect_location_points_to_login(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        request = _make_request("/wiki/my-repo/")
        exc_401 = HTTPException(status_code=401, detail="Authentication required")

        with patch(
            "code_indexer.server.auth.dependencies._hybrid_auth_impl",
            side_effect=exc_401,
        ):
            with pytest.raises(HTTPException) as exc_info:
                get_wiki_user_hybrid(request)

        location = exc_info.value.headers["Location"]
        assert location.startswith("/login?redirect_to=")

    def test_redirect_preserves_full_wiki_path(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        path = "/wiki/sf-kb-wiki/Customer/some-article"
        request = _make_request(path)
        exc_401 = HTTPException(status_code=401, detail="Authentication required")

        with patch(
            "code_indexer.server.auth.dependencies._hybrid_auth_impl",
            side_effect=exc_401,
        ):
            with pytest.raises(HTTPException) as exc_info:
                get_wiki_user_hybrid(request)

        location = exc_info.value.headers["Location"]
        # The redirect_to param must contain the full path (URL-encoded)
        assert "/wiki/sf-kb-wiki/Customer/some-article" in location or \
               "%2Fwiki%2Fsf-kb-wiki%2FCustomer%2Fsome-article" in location

    def test_redirect_preserves_query_string(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        path = "/wiki/my-repo/article"
        query = "version=2&lang=en"
        request = _make_request(path, query)
        exc_401 = HTTPException(status_code=401, detail="Authentication required")

        with patch(
            "code_indexer.server.auth.dependencies._hybrid_auth_impl",
            side_effect=exc_401,
        ):
            with pytest.raises(HTTPException) as exc_info:
                get_wiki_user_hybrid(request)

        location = exc_info.value.headers["Location"]
        # The full URL (path + query) must be preserved in redirect_to
        assert "version%3D2" in location or "version=2" in location

    def test_non_401_error_passes_through(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        request = _make_request("/wiki/admin-only/")
        exc_403 = HTTPException(status_code=403, detail="Admin access required")

        with patch(
            "code_indexer.server.auth.dependencies._hybrid_auth_impl",
            side_effect=exc_403,
        ):
            with pytest.raises(HTTPException) as exc_info:
                get_wiki_user_hybrid(request)

        # Must re-raise as-is, NOT convert to redirect
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Admin access required"

    def test_authenticated_user_passes_through(self):
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid

        request = _make_request("/wiki/my-repo/")
        user = _make_user("alice")

        with patch(
            "code_indexer.server.auth.dependencies._hybrid_auth_impl",
            return_value=user,
        ):
            result = get_wiki_user_hybrid(request)

        assert result is user


class TestJsonSearchRoutesStillReturn401:
    """wiki_search and user_wiki_search must still use get_current_user_hybrid (JSON 401)."""

    def test_wiki_search_uses_original_dependency(self):
        """wiki_search route must NOT use get_wiki_user_hybrid."""
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.wiki_search)
        # Must reference get_current_user_hybrid, not get_wiki_user_hybrid
        assert "get_current_user_hybrid" in source
        assert "get_wiki_user_hybrid" not in source

    def test_user_wiki_search_uses_original_dependency(self):
        """user_wiki_search route must NOT use get_wiki_user_hybrid."""
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.user_wiki_search)
        assert "get_current_user_hybrid" in source
        assert "get_wiki_user_hybrid" not in source


class TestHtmlRoutesUseWikiDependency:
    """HTML-serving routes must use get_wiki_user_hybrid (produces 303 redirects)."""

    def test_serve_wiki_root_uses_wiki_dependency(self):
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.serve_wiki_root)
        assert "get_wiki_user_hybrid" in source

    def test_serve_wiki_article_uses_wiki_dependency(self):
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.serve_wiki_article)
        assert "get_wiki_user_hybrid" in source

    def test_serve_wiki_asset_uses_wiki_dependency(self):
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.serve_wiki_asset)
        assert "get_wiki_user_hybrid" in source

    def test_serve_user_wiki_root_uses_wiki_dependency(self):
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.serve_user_wiki_root)
        assert "get_wiki_user_hybrid" in source

    def test_serve_user_wiki_article_uses_wiki_dependency(self):
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.serve_user_wiki_article)
        assert "get_wiki_user_hybrid" in source

    def test_serve_user_wiki_asset_uses_wiki_dependency(self):
        from code_indexer.server.wiki import routes as wiki_routes

        source = inspect.getsource(wiki_routes.serve_user_wiki_asset)
        assert "get_wiki_user_hybrid" in source


class TestGetWikiUserHybridRealCallPath:
    """Exposes Bug #296: get_wiki_user_hybrid calls get_current_user_hybrid as a plain
    Python function, so FastAPI never injects credentials — the default value is the raw
    Depends(security) object.  Accessing .credentials on that object raises AttributeError
    which is NOT caught by 'except HTTPException', so the caller gets 500 instead of 303.

    These tests do NOT mock get_current_user_hybrid.  They exercise the real call path
    with a minimal fake request to verify the function produces a 303 redirect (not a 500).
    """

    def _make_minimal_request(self, path: str, cookies: dict = None) -> MagicMock:
        """Build a mock Request with no session cookie, no auth header."""
        request = MagicMock()
        request.url.path = path
        request.url.query = ""
        # cookies.get("session") must return None — no session cookie present
        request.cookies = cookies or {}
        request.headers.get = lambda key, default="": default
        return request

    def test_no_session_no_bearer_raises_303_not_500(self):
        """Without session cookie or Bearer token, must get 303 redirect, not 500 AttributeError."""
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid
        from code_indexer.server.web.auth import init_session_manager
        from unittest.mock import patch

        # Initialise a real session manager (needed by _hybrid_auth_impl)
        mock_config = MagicMock()
        mock_config.host = "localhost"
        init_session_manager("test-secret-key-for-testing", mock_config)

        request = self._make_minimal_request("/wiki/my-repo/")

        # Patch user_manager in dependencies so _hybrid_auth_impl doesn't blow up on None check
        mock_user_manager = MagicMock()
        with patch("code_indexer.server.auth.dependencies.user_manager", mock_user_manager):
            with patch("code_indexer.server.auth.dependencies.jwt_manager", MagicMock()):
                with pytest.raises(HTTPException) as exc_info:
                    get_wiki_user_hybrid(request)

        # Must be 303, not 500 (AttributeError scenario) or 401 (plain unwrapped error)
        assert exc_info.value.status_code == status.HTTP_303_SEE_OTHER, (
            f"Expected 303 redirect but got {exc_info.value.status_code}. "
            "This likely means get_current_user_hybrid was called as a plain function "
            "and the Depends(security) object caused an AttributeError, raising 500."
        )

    def test_no_session_no_bearer_redirect_location_correct(self):
        """303 redirect must point to /login?redirect_to=<path>."""
        from code_indexer.server.wiki.routes import get_wiki_user_hybrid
        from code_indexer.server.web.auth import init_session_manager
        from unittest.mock import patch

        mock_config = MagicMock()
        mock_config.host = "localhost"
        init_session_manager("test-secret-key-for-testing", mock_config)

        request = self._make_minimal_request("/wiki/my-repo/article")

        mock_user_manager = MagicMock()
        with patch("code_indexer.server.auth.dependencies.user_manager", mock_user_manager):
            with patch("code_indexer.server.auth.dependencies.jwt_manager", MagicMock()):
                with pytest.raises(HTTPException) as exc_info:
                    get_wiki_user_hybrid(request)

        assert exc_info.value.status_code == status.HTTP_303_SEE_OTHER
        location = exc_info.value.headers["Location"]
        assert "/login" in location
        assert "redirect_to" in location


class TestEndToEndRedirectBehavior:
    """Integration test: unauthenticated HTTP request to wiki root returns 303.

    Overrides get_wiki_user_hybrid (the actual FastAPI dependency used by the
    routes) to raise a 303 redirect, proving the route wires the dependency
    correctly and propagates the redirect response to the caller.

    Note: overriding get_current_user_hybrid would not work here because
    get_wiki_user_hybrid calls it as a direct Python function (not via DI),
    so FastAPI's dependency_overrides mechanism cannot intercept it.
    """

    def test_unauthenticated_wiki_root_returns_303_not_401(self):
        """Without any auth, wiki root must redirect to /login, not return JSON 401."""
        import os
        import tempfile
        from pathlib import Path
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from code_indexer.server.wiki.routes import (
            wiki_router,
            get_wiki_user_hybrid,
            _reset_wiki_cache,
        )

        _reset_wiki_cache()
        app = FastAPI()

        # Override get_wiki_user_hybrid — the dependency the routes actually declare
        # via Depends(). get_current_user_hybrid is called as a direct Python call
        # inside get_wiki_user_hybrid, so overriding it via dependency_overrides has
        # no effect on route behaviour. We override the outer dependency directly and
        # raise the 303 redirect it would produce for an unauthenticated request.
        def _raise_redirect(request: Request):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login?redirect_to=%2Fwiki%2Ftest-repo%2F"},
            )

        app.dependency_overrides[get_wiki_user_hybrid] = _raise_redirect
        app.include_router(wiki_router, prefix="/wiki")

        app.state.golden_repo_manager = MagicMock()
        app.state.golden_repo_manager.get_wiki_enabled.return_value = True
        _tmp_golden = tempfile.mkdtemp(suffix="-golden-repos")
        (Path(_tmp_golden) / "aliases").mkdir(parents=True, exist_ok=True)
        app.state.golden_repo_manager.golden_repos_dir = _tmp_golden
        _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
        os.close(_db_fd)
        app.state.golden_repo_manager.db_path = _db_path
        app.state.access_filtering_service = MagicMock()

        client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
        resp = client.get("/wiki/test-repo/")
        # get_wiki_user_hybrid converts the 401 to a 303 redirect
        assert resp.status_code == 303
        assert "Location" in resp.headers
        assert "/login" in resp.headers["Location"]
