"""Route integration tests for wiki navigation (Story #282)."""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_app(authenticated_user, actual_repo_path, user_accessible_repos=None):
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: authenticated_user
    app.dependency_overrides[get_current_user_hybrid] = lambda: authenticated_user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = True
    app.state.golden_repo_manager.get_actual_repo_path.return_value = actual_repo_path
    app.state.golden_repo_manager.db_path = _db_path

    # Create alias infrastructure for AliasManager resolution (Story #286)
    golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
    app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    app.state.access_filtering_service.get_accessible_repos.return_value = user_accessible_repos or set()
    return app


class TestRoutesNavigationIntegration:
    def test_sidebar_appears_in_article_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "home.md").write_text("# Home")
            (repo_dir / "guide.md").write_text("# Guide")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/guide")
            assert resp.status_code == 200
            # Sidebar should contain a link to guide
            assert "guide" in resp.text.lower()

    def test_breadcrumbs_appear_in_article_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            subdir = repo_dir / "docs"
            subdir.mkdir()
            (subdir / "intro.md").write_text("# Intro")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/docs/intro")
            assert resp.status_code == 200
            # Breadcrumbs should show path segments
            assert "Wiki Home" in resp.text

    def test_active_article_marked_in_sidebar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "article-one.md").write_text("# Article One")
            (repo_dir / "article-two.md").write_text("# Article Two")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/article-one")
            assert resp.status_code == 200
            # active class should be present for the current article
            assert "active" in resp.text

    def test_home_md_renders_as_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "home.md").write_text("# Welcome\nThis is home content.")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200
            assert "home content" in resp.text.lower()

    def test_article_title_in_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "my-page.md").write_text("# My Custom Title\nContent here.")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/my-page")
            assert resp.status_code == 200
            assert "My Custom Title" in resp.text

    def test_nested_article_accessible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            section = repo_dir / "section"
            section.mkdir()
            (section / "deep-page.md").write_text("# Deep Page\nDeep content.")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/section/deep-page")
            assert resp.status_code == 200
            assert "Deep content" in resp.text
