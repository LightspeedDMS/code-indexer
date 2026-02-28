"""Tests for Story #290: Semantic and FTS Search Filtering for wiki module.

AC1: Search Box UI — single search box with Semantic/FTS dropdown at top of sidebar
AC2: Backend Search Endpoint — GET /wiki/{repo_alias}/_search?q=...&mode=semantic|fts
AC3: TOC Filtering to Matches — hide non-matching sections/articles, expand matching ones
AC4: First Match Auto-Loads — navigate to highest-scoring result
AC5: Clear Button Resets TOC — restore full TOC state
AC6: Debounced Input — 300ms debounce, min 2 chars, cancel pending requests
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


def _make_user(username: str):
    user = MagicMock()
    user.username = username
    return user


def _make_search_app(
    authenticated_user=None,
    actual_repo_path=None,
    user_accessible_repos=None,
    wiki_enabled=True,
    semantic_query_manager=None,
):
    """Build a test FastAPI app with wiki router and optional SemanticQueryManager."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    if authenticated_user:
        app.dependency_overrides[get_wiki_user_hybrid] = lambda: authenticated_user
        app.dependency_overrides[get_current_user_hybrid] = lambda: authenticated_user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = wiki_enabled
    app.state.golden_repo_manager.db_path = _db_path

    if actual_repo_path:
        golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-search-test"
        golden_repos_dir.mkdir(parents=True, exist_ok=True)
        make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
        app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)
    else:
        _tmp_golden = tempfile.mkdtemp(suffix="-golden-repos")
        (Path(_tmp_golden) / "aliases").mkdir(parents=True, exist_ok=True)
        app.state.golden_repo_manager.golden_repos_dir = _tmp_golden

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    app.state.access_filtering_service.get_accessible_repos.return_value = (
        user_accessible_repos or {"test-repo"}
    )

    if semantic_query_manager is not None:
        app.state.semantic_query_manager = semantic_query_manager
    # If not provided, app.state will not have the attribute (tests graceful degradation)

    return app


def _make_mock_query_manager(results=None):
    """Build a mock SemanticQueryManager with configurable return values."""
    manager = MagicMock()
    if results is None:
        results = [
            {
                "file_path": "guides/getting-started.md",
                "similarity_score": 0.92,
                "code_snippet": "Getting started with the system",
            },
            {
                "file_path": "reference/api-overview.md",
                "similarity_score": 0.85,
                "code_snippet": "API overview and endpoints",
            },
        ]
    # query_user_repositories returns dict with "results" key
    manager.query_user_repositories.return_value = {"results": results}
    return manager


# ---------------------------------------------------------------------------
# Backend Tests: AC2 — Search Endpoint behaviour
# ---------------------------------------------------------------------------


class TestSearchEndpointBasic:
    """AC2: GET /wiki/{repo_alias}/_search endpoint correctness."""

    def test_search_endpoint_returns_json_for_semantic_mode(self):
        """Semantic mode returns JSON array with path/score/title."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=getting+started&mode=semantic")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) > 0
            first = data[0]
            assert "path" in first
            assert "score" in first
            assert "title" in first

    def test_search_endpoint_returns_json_for_fts_mode(self):
        """FTS mode passes search_mode='fts' to query manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=api+overview&mode=fts")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            # Verify fts mode was passed to the query manager
            call_kwargs = mock_qm.query_user_repositories.call_args
            assert call_kwargs is not None
            # search_mode should be "fts"
            passed_mode = (
                call_kwargs.kwargs.get("search_mode")
                or call_kwargs.args[4]
                if len(call_kwargs.args) > 4
                else None
            )
            assert passed_mode == "fts" or call_kwargs.kwargs.get("search_mode") == "fts"

    def test_search_endpoint_returns_empty_for_short_query(self):
        """Query shorter than 2 characters returns empty array without calling manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=a&mode=semantic")
            assert resp.status_code == 200
            assert resp.json() == []
            mock_qm.query_user_repositories.assert_not_called()

    def test_search_endpoint_returns_empty_for_empty_query(self):
        """Empty query returns empty array without calling manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=&mode=semantic")
            assert resp.status_code == 200
            assert resp.json() == []
            mock_qm.query_user_repositories.assert_not_called()

    def test_search_endpoint_default_mode_is_semantic(self):
        """When mode is omitted, defaults to semantic search."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=getting+started")
            assert resp.status_code == 200
            call_kwargs = mock_qm.query_user_repositories.call_args
            assert call_kwargs is not None
            assert call_kwargs.kwargs.get("search_mode") == "semantic"


class TestSearchEndpointAuth:
    """Authentication requirements for the search endpoint."""

    def test_search_endpoint_requires_auth(self):
        """Search endpoint returns 401/403 without authentication."""
        app = FastAPI()
        app.include_router(wiki_router, prefix="/wiki")
        app.state.golden_repo_manager = MagicMock()
        app.state.access_filtering_service = MagicMock()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/test-repo/_search?q=hello")
        assert resp.status_code in (401, 403)

    def test_search_endpoint_404_for_nonexistent_repo(self):
        """Search on non-activated repo returns 404 (invisible repo)."""
        # No actual_repo_path means aliases dir is empty -> AliasManager returns None -> 404
        app = _make_search_app(
            _make_user("alice"),
            actual_repo_path=None,
            wiki_enabled=True,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/test-repo/_search?q=hello+world")
        assert resp.status_code == 404

    def test_search_endpoint_wiki_disabled_returns_404(self):
        """Search on wiki-disabled repo returns 404."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_search_app(
                _make_user("alice"),
                actual_repo_path=tmpdir,
                wiki_enabled=False,
            )
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/wiki/test-repo/_search?q=hello+world")
            assert resp.status_code == 404


class TestSearchEndpointResultMapping:
    """Result mapping: path stripping, title extraction, score."""

    def test_search_endpoint_filters_md_files_only(self):
        """file_extensions=['.md'] is passed to query manager to filter markdown only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            client.get("/wiki/test-repo/_search?q=test+query")
            call_kwargs = mock_qm.query_user_repositories.call_args
            assert call_kwargs is not None
            file_extensions = call_kwargs.kwargs.get("file_extensions")
            assert file_extensions is not None
            assert ".md" in file_extensions

    def test_search_endpoint_strips_md_extension_from_path(self):
        """Result paths have .md extension stripped to match wiki URL paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager(results=[
                {
                    "file_path": "guides/getting-started.md",
                    "similarity_score": 0.9,
                    "code_snippet": "snippet",
                }
            ])
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=getting+started")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            # Path must not end with .md
            assert not data[0]["path"].endswith(".md")
            assert data[0]["path"] == "guides/getting-started"

    def test_search_response_format(self):
        """Each result has exactly the required keys: path, score, title."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager(results=[
                {
                    "file_path": "reference/api-overview.md",
                    "similarity_score": 0.85,
                    "code_snippet": "API endpoints",
                }
            ])
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=api+overview")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            result = data[0]
            assert set(result.keys()) >= {"path", "score", "title"}
            assert result["path"] == "reference/api-overview"
            assert abs(result["score"] - 0.85) < 0.001
            # Title is derived from filename stem (title-cased, dashes to spaces)
            assert isinstance(result["title"], str)
            assert len(result["title"]) > 0

    def test_search_endpoint_handles_query_manager_error(self):
        """When query manager raises, endpoint returns graceful error (200, not 500)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = MagicMock()
            mock_qm.query_user_repositories.side_effect = RuntimeError("Search backend down")
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=test+query")
            # Must return 200 with error field, not crash the page
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data


# ---------------------------------------------------------------------------
# Frontend Tests: HTML structure (AC1, AC5)
# ---------------------------------------------------------------------------


class TestArticleTemplateSearchBox:
    """AC1: Search box UI exists in the sidebar."""

    def _get_article_html(self, tmpdir_path: str) -> str:
        """Helper: render article.html and return response text."""
        Path(tmpdir_path, "home.md").write_text("# Home\nContent")
        mock_qm = _make_mock_query_manager()
        app = _make_search_app(
            _make_user("alice"), tmpdir_path, semantic_query_manager=mock_qm
        )
        client = TestClient(app)
        resp = client.get("/wiki/test-repo/")
        assert resp.status_code == 200
        return resp.text

    def test_article_template_has_search_box(self):
        """Search input element exists inside the sidebar."""
        with tempfile.TemporaryDirectory() as tmpdir:
            html = self._get_article_html(tmpdir)
            assert 'id="wiki-search-input"' in html
            assert 'type="text"' in html

    def test_article_template_has_mode_picker(self):
        """Mode dropdown with semantic/fts options exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            html = self._get_article_html(tmpdir)
            assert 'id="wiki-search-mode"' in html
            assert 'value="semantic"' in html
            assert 'value="fts"' in html

    def test_article_template_has_clear_button(self):
        """Clear button (X) exists in the search box."""
        with tempfile.TemporaryDirectory() as tmpdir:
            html = self._get_article_html(tmpdir)
            assert 'id="wiki-search-clear"' in html

    def test_article_template_search_box_before_toc(self):
        """Search box HTML appears before the first sidebar-group in the document."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Add an article to ensure sidebar-group is rendered
            Path(tmpdir, "guide.md").write_text("---\ncategory: Guides\n---\n# Guide")
            Path(tmpdir, "home.md").write_text("# Home")
            html = self._get_article_html(tmpdir)
            search_pos = html.find('id="wiki-search-box"')
            toc_pos = html.find('class="sidebar-group"')
            assert search_pos != -1, "wiki-search-box not found in HTML"
            assert toc_pos != -1, "sidebar-group not found in HTML"
            assert search_pos < toc_pos, (
                "Search box must appear before the first sidebar-group"
            )

    def test_sidebar_items_have_data_path(self):
        """All sidebar-item links have data-path attribute."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create articles so sidebar items are generated
            Path(tmpdir, "home.md").write_text("# Home")
            Path(tmpdir, "guide.md").write_text("---\ncategory: Guides\n---\n# Guide")
            Path(tmpdir, "ref.md").write_text("---\ncategory: Reference\n---\n# Ref")
            html = self._get_article_html(tmpdir)
            # Find all sidebar-item anchor tags
            import re
            # All sidebar-item anchors should have data-path
            sidebar_items = re.findall(
                r'<a[^>]+class="[^"]*sidebar-item[^"]*"[^>]*>', html
            )
            assert len(sidebar_items) > 0, "No sidebar items found in HTML"
            for item_tag in sidebar_items:
                assert 'data-path="' in item_tag, (
                    f"sidebar-item missing data-path attribute: {item_tag}"
                )


# ---------------------------------------------------------------------------
# Route Order Test: AC2 critical correctness
# ---------------------------------------------------------------------------


class TestSearchRouteOrdering:
    """The _search endpoint must not be caught by the catch-all /{path:path} route."""

    def test_search_route_not_caught_by_article_catch_all(self):
        """GET /wiki/{alias}/_search returns JSON (not 404 or HTML article page)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_qm = _make_mock_query_manager()
            app = _make_search_app(
                _make_user("alice"), tmpdir, semantic_query_manager=mock_qm
            )
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_search?q=hello+world")
            assert resp.status_code == 200
            # Must be JSON, not HTML
            content_type = resp.headers.get("content-type", "")
            assert "application/json" in content_type, (
                f"Expected JSON response, got content-type: {content_type}. "
                "This means _search was caught by the article catch-all route."
            )
            # Must be a list (array of results)
            data = resp.json()
            assert isinstance(data, list) or isinstance(data, dict)
