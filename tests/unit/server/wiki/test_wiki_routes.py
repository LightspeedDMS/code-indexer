"""Tests for wiki routes (Stories #280, #281)."""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import wiki_router, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_app(authenticated_user=None, actual_repo_path=None,
              user_accessible_repos=None, wiki_enabled=True):
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    if authenticated_user:
        app.dependency_overrides[get_current_user_hybrid] = lambda: authenticated_user
    app.include_router(wiki_router, prefix="/wiki")

    # Mount wiki static for template rendering
    from fastapi.staticfiles import StaticFiles
    wiki_static = Path(__file__).parent / "../../../../src/code_indexer/server/wiki/static"
    if wiki_static.exists():
        app.mount("/wiki/_static", StaticFiles(directory=str(wiki_static.resolve())), name="wiki_static")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = wiki_enabled
    app.state.golden_repo_manager.get_actual_repo_path.return_value = actual_repo_path or "/tmp/test"
    app.state.golden_repo_manager.db_path = _db_path

    # Create alias infrastructure for AliasManager resolution.
    # Production code uses AliasManager.read_alias("{alias}-global") instead of
    # get_actual_repo_path(), so golden_repos_dir + aliases/{alias}-global.json must exist.
    if actual_repo_path:
        golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test"
        golden_repos_dir.mkdir(parents=True, exist_ok=True)
        make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
        app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)
    else:
        # No repo path - create empty aliases dir so AliasManager can be constructed
        # but will find no alias file -> returns None -> 404 (correct behavior)
        _tmp_golden = tempfile.mkdtemp(suffix="-golden-repos")
        (Path(_tmp_golden) / "aliases").mkdir(parents=True, exist_ok=True)
        app.state.golden_repo_manager.golden_repos_dir = _tmp_golden

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = False
    app.state.access_filtering_service.get_accessible_repos.return_value = user_accessible_repos or set()
    return app


class TestAuthentication:
    def test_unauthenticated_returns_401(self):
        app = FastAPI()
        app.include_router(wiki_router, prefix="/wiki")
        app.state.golden_repo_manager = MagicMock()
        app.state.access_filtering_service = MagicMock()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/test-repo/")
        assert resp.status_code in (401, 403)

    def test_authenticated_reaches_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Home")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            assert client.get("/wiki/test-repo/").status_code == 200


class TestAccessControl:
    def test_disabled_wiki_returns_404(self):
        app = _make_app(_make_user("alice"), wiki_enabled=False, user_accessible_repos={"test-repo"})
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/wiki/test-repo/").status_code == 404

    def test_no_group_access_returns_404(self):
        app = _make_app(_make_user("alice"), user_accessible_repos=set())
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/wiki/test-repo/").status_code == 404

    def test_admin_bypasses_group_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Home")
            app = _make_app(_make_user("admin"), tmpdir, set())
            app.state.access_filtering_service.is_admin_user.return_value = True
            client = TestClient(app)
            assert client.get("/wiki/test-repo/").status_code == 200

    def test_nonexistent_repo_returns_404(self):
        app = _make_app(_make_user("alice"), user_accessible_repos={"test-repo"})
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/wiki/test-repo/").status_code == 404


class TestArticleServing:
    def test_valid_article_returns_html(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "test.md").write_text("# Test\nHello world")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/test")
            assert resp.status_code == 200
            assert "Hello world" in resp.text
            assert "text/html" in resp.headers.get("content-type", "")

    def test_md_extension_auto_resolved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "article.md").write_text("# Article")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            assert client.get("/wiki/test-repo/article").status_code == 200

    def test_nonexistent_article_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/missing").status_code == 404

    def test_non_markdown_file_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "secret.env").write_text("PASSWORD=x")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/secret.env").status_code == 404


class TestRootPage:
    def test_serves_home_md(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "home.md").write_text("# Welcome\nHome content")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200
            assert "Home content" in resp.text

    def test_index_when_no_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "page1.md").write_text("# Page 1")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200


class TestImageServing:
    def test_assets_route_serves_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dir = Path(tmpdir, "uploads")
            img_dir.mkdir()
            (img_dir / "test.png").write_bytes(b'\x89PNG\r\n')
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/_assets/uploads/test.png")
            assert resp.status_code == 200

    def test_assets_missing_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/_assets/missing.png").status_code == 404


class TestSecurity:
    def test_path_traversal_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            app.state.access_filtering_service.is_admin_user.return_value = True
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/../../etc/passwd").status_code == 404

    def test_value_error_alias_returns_404(self):
        app = _make_app(_make_user("alice"), user_accessible_repos={"../evil"})
        app.state.golden_repo_manager.get_actual_repo_path.side_effect = ValueError("bad")
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/wiki/../evil/").status_code == 404


class TestAssetAllowlist:
    """H1: Asset route must reject non-allowlisted file types."""

    def test_image_png_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "img.png").write_bytes(b'\x89PNG\r\n')
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            assert client.get("/wiki/test-repo/_assets/img.png").status_code == 200

    def test_svg_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "icon.svg").write_bytes(b'<svg/>')
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            assert client.get("/wiki/test-repo/_assets/icon.svg").status_code == 200

    def test_env_file_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "secret.env").write_text("PASSWORD=x")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/_assets/secret.env").status_code == 404

    def test_py_file_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "exploit.py").write_text("import os")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/_assets/exploit.py").status_code == 404

    def test_sql_file_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "dump.sql").write_text("SELECT * FROM users")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/wiki/test-repo/_assets/dump.sql").status_code == 404

    def test_css_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            assert client.get("/wiki/test-repo/_assets/style.css").status_code == 200


class TestRootPageHiddenDirFiltering:
    """M1: rglob must skip hidden/internal directories like .git, .code-indexer."""

    def test_hidden_dir_files_excluded_from_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "visible.md").write_text("# Visible")
            hidden = repo_dir / ".git"
            hidden.mkdir()
            (hidden / "config.md").write_text("# Git internals")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200
            assert ".git" not in resp.text

    def test_code_indexer_dir_excluded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "readme.md").write_text("# Readme")
            internal = repo_dir / ".code-indexer"
            internal.mkdir()
            (internal / "internal.md").write_text("# Internal")
            app = _make_app(_make_user("alice"), tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/")
            assert resp.status_code == 200
            assert ".code-indexer" not in resp.text
