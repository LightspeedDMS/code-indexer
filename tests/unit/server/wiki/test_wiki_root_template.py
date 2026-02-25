"""Tests for wiki root page template selection (serve_wiki_root).

Verifies that when no home.md exists, the root page renders using the full
article.html template (with sidebar, toolbar, breadcrumbs) instead of the
bare index.html template.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_app(actual_repo_path=None, user_accessible_repos=None, wiki_enabled=True):
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    user = _make_user("alice")
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = wiki_enabled
    app.state.golden_repo_manager.db_path = _db_path

    if actual_repo_path:
        golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test"
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
    return app


class TestRootPageTemplateWhenNoHomeMd:
    """When no home.md exists, root page must use full wiki UI (article.html)."""

    def test_uses_article_html_template_not_index_html(self):
        """Root page without home.md must not render bare index.html list.

        article.html contains the sidebar nav element (id="wiki-sidebar");
        index.html does not. We confirm that markup is present in the response.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page1.md").write_text("# Page One\nSome content")
            (repo_dir / "page2.md").write_text("# Page Two\nOther content")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            # article.html always renders the sidebar nav; index.html never does
            assert 'id="wiki-sidebar"' in resp.text

    def test_article_listing_content_is_present(self):
        """Root page without home.md must list available articles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "my-guide.md").write_text("# My Guide")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            # The article listing link should contain the article path
            assert "my-guide" in resp.text

    def test_response_uses_full_wiki_chrome(self):
        """Root page without home.md must render full wiki chrome from article.html.

        article.html always renders the toolbar and sidebar nav;
        index.html renders neither. The wiki-toolbar div is unconditional in article.html.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "article.md").write_text("# Article")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            # article.html unconditionally renders the toolbar and sidebar
            assert 'id="wiki-sidebar"' in resp.text
            assert 'class="wiki-toolbar"' in resp.text

    def test_article_links_point_to_correct_wiki_paths(self):
        """Article links in the listing must use /wiki/{repo_alias}/{path} format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "getting-started.md").write_text("# Getting Started")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            # Link must include the repo alias and the article path
            assert "test-repo" in resp.text
            assert "getting-started" in resp.text

    def test_hidden_dir_files_excluded_from_listing(self):
        """Articles inside hidden directories must not appear in the listing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "visible.md").write_text("# Visible")
            hidden = repo_dir / ".git"
            hidden.mkdir()
            (hidden / "config.md").write_text("# Git internals")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            assert ".git" not in resp.text

    def test_title_formatted_from_stem(self):
        """Article titles derived from filenames must be title-cased with dashes/underscores replaced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "quick-start-guide.md").write_text("# Quick Start Guide")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            # Title derived from stem: "quick-start-guide" -> "Quick Start Guide"
            assert "Quick Start Guide" in resp.text


class TestRootPageTemplateWhenHomeMdExists:
    """When home.md exists, behavior must be unchanged (article.html rendered directly)."""

    def test_home_md_renders_content_via_article_html(self):
        """home.md content must be rendered with full wiki UI (article.html)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Welcome\nHome page content here")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            assert "Home page content here" in resp.text
            # article.html always renders the sidebar nav
            assert 'id="wiki-sidebar"' in resp.text

    def test_home_md_does_not_show_article_listing(self):
        """When home.md exists, the article listing HTML must NOT be the main content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "home.md").write_text("# Welcome\nHome content")
            (Path(tmpdir) / "other.md").write_text("# Other")

            app = _make_app(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")

            assert resp.status_code == 200
            assert "Home content" in resp.text
            # The listing header "Available Articles" must not appear when home.md exists
            assert "Available Articles" not in resp.text
