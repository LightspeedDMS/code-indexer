"""Tests for Story #286: Wiki Reads Through Global Repo Alias System.

TDD red phase - AC1 and AC2 (core behavioral changes).

AC1 - Wiki path resolution through AliasManager.read_alias("{alias}-global")
AC2 - Missing alias returns 404 with no fallback to get_actual_repo_path()
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(username: str):
    user = MagicMock()
    user.username = username
    return user


def _make_app(*, authenticated_user, golden_repos_dir: str,
              wiki_enabled: bool = True, is_admin: bool = True,
              user_accessible_repos=None):
    """Create a FastAPI test app wired to wiki_router with alias-aware state."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: authenticated_user
    app.dependency_overrides[get_current_user_hybrid] = lambda: authenticated_user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    manager = MagicMock()
    manager.get_wiki_enabled.return_value = wiki_enabled
    manager.db_path = _db_path
    manager.golden_repos_dir = golden_repos_dir

    app.state.golden_repo_manager = manager
    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = is_admin
    app.state.access_filtering_service.get_accessible_repos.return_value = (
        user_accessible_repos if user_accessible_repos is not None else set()
    )
    return app


# ---------------------------------------------------------------------------
# AC1: Wiki path resolution through AliasManager
# ---------------------------------------------------------------------------

class TestAC1AliasManagerPathResolution:
    """Routes resolve repo path via AliasManager.read_alias('{alias}-global')."""

    def test_root_page_resolves_via_alias(self):
        """GET /wiki/{alias}/ reads path from alias JSON, not get_actual_repo_path."""
        with tempfile.TemporaryDirectory() as base:
            repo_dir = Path(base) / "repo"
            repo_dir.mkdir()
            (repo_dir / "home.md").write_text("# Home via alias")
            golden_repos_dir = Path(base) / "golden-repos"
            golden_repos_dir.mkdir()
            make_aliases_dir(str(golden_repos_dir), "my-repo", str(repo_dir))

            app = _make_app(authenticated_user=_make_user("admin"),
                            golden_repos_dir=str(golden_repos_dir))
            client = TestClient(app)
            resp = client.get("/wiki/my-repo/")
            assert resp.status_code == 200
            assert "Home via alias" in resp.text

    def test_article_page_resolves_via_alias(self):
        """GET /wiki/{alias}/{path} reads article from alias-resolved directory."""
        with tempfile.TemporaryDirectory() as base:
            repo_dir = Path(base) / "repo"
            repo_dir.mkdir()
            (repo_dir / "guide.md").write_text("# Guide\nAlias content")
            golden_repos_dir = Path(base) / "golden-repos"
            golden_repos_dir.mkdir()
            make_aliases_dir(str(golden_repos_dir), "my-repo", str(repo_dir))

            app = _make_app(authenticated_user=_make_user("admin"),
                            golden_repos_dir=str(golden_repos_dir))
            client = TestClient(app)
            resp = client.get("/wiki/my-repo/guide")
            assert resp.status_code == 200
            assert "Alias content" in resp.text

    def test_get_actual_repo_path_never_called_when_alias_exists(self):
        """After the change, get_actual_repo_path() must NOT be called for path resolution."""
        with tempfile.TemporaryDirectory() as base:
            repo_dir = Path(base) / "repo"
            repo_dir.mkdir()
            (repo_dir / "home.md").write_text("# Home")
            golden_repos_dir = Path(base) / "golden-repos"
            golden_repos_dir.mkdir()
            make_aliases_dir(str(golden_repos_dir), "my-repo", str(repo_dir))

            app = _make_app(authenticated_user=_make_user("admin"),
                            golden_repos_dir=str(golden_repos_dir))
            client = TestClient(app)
            resp = client.get("/wiki/my-repo/")
            assert resp.status_code == 200
            app.state.golden_repo_manager.get_actual_repo_path.assert_not_called()


# ---------------------------------------------------------------------------
# AC2: Missing alias returns 404, no fallback
# ---------------------------------------------------------------------------

class TestAC2MissingAlias404NoFallback:
    """When alias JSON is absent, return 404 without touching get_actual_repo_path()."""

    def test_missing_alias_returns_404_root(self):
        """No alias file -> 404 for root page."""
        with tempfile.TemporaryDirectory() as base:
            golden_repos_dir = Path(base) / "golden-repos"
            golden_repos_dir.mkdir()
            (Path(golden_repos_dir) / "aliases").mkdir(parents=True, exist_ok=True)

            app = _make_app(authenticated_user=_make_user("admin"),
                            golden_repos_dir=str(golden_repos_dir))
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/wiki/no-such-repo/")
            assert resp.status_code == 404

    def test_missing_alias_returns_404_article(self):
        """No alias file -> 404 for article page."""
        with tempfile.TemporaryDirectory() as base:
            golden_repos_dir = Path(base) / "golden-repos"
            golden_repos_dir.mkdir()
            (Path(golden_repos_dir) / "aliases").mkdir(parents=True, exist_ok=True)

            app = _make_app(authenticated_user=_make_user("admin"),
                            golden_repos_dir=str(golden_repos_dir))
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/wiki/no-such-repo/some-article")
            assert resp.status_code == 404

    def test_missing_alias_does_not_call_get_actual_repo_path(self):
        """404 must NOT fall back to get_actual_repo_path() for path resolution."""
        with tempfile.TemporaryDirectory() as base:
            golden_repos_dir = Path(base) / "golden-repos"
            golden_repos_dir.mkdir()
            (Path(golden_repos_dir) / "aliases").mkdir(parents=True, exist_ok=True)

            app = _make_app(authenticated_user=_make_user("admin"),
                            golden_repos_dir=str(golden_repos_dir))
            client = TestClient(app, raise_server_exceptions=False)
            client.get("/wiki/no-such-repo/")
            manager = app.state.golden_repo_manager
            manager.get_actual_repo_path.assert_not_called()
