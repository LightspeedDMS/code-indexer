"""Tests for user-activated repo wiki support (Story #291).

Covers:
  - ActivatedRepoManager.get_wiki_enabled / set_wiki_enabled methods
  - User wiki route access control (owner + admin only, 404 for others)
  - User wiki content served from activated repo path
  - WikiCache isolation (u:{username}:{alias} prefix)
  - Web UI toggle endpoint
  - Asset and search routes for user wikis
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.wiki.routes import wiki_router, get_current_user_hybrid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(username: str):
    user = MagicMock()
    user.username = username
    return user


def _write_metadata(user_dir: str, alias: str, extra: Optional[dict] = None) -> str:
    """Write minimal _metadata.json for an activated repo and return metadata path."""
    os.makedirs(user_dir, exist_ok=True)
    repo_dir = os.path.join(user_dir, alias)
    os.makedirs(repo_dir, exist_ok=True)
    metadata: dict = {
        "user_alias": alias,
        "golden_repo_alias": "golden-repo",
        "current_branch": "main",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
    }
    if extra:
        metadata.update(extra)
    metadata_path = os.path.join(user_dir, f"{alias}_metadata.json")
    with open(metadata_path, "w") as f:
        json.dump(metadata, f)
    return metadata_path


def _make_user_wiki_app(
    *,
    username: str = "alice",
    alias: str = "my-repo",
    repo_path: Optional[str] = None,
    wiki_enabled: bool = True,
    current_user=None,
    is_admin: bool = False,
    viewer_username: Optional[str] = None,
):
    """Build a minimal FastAPI test app with wiki_router for user wiki tests."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    viewer = current_user or _make_user(viewer_username or username)

    app = FastAPI()
    app.dependency_overrides[get_current_user_hybrid] = lambda: viewer
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    # Golden repo manager mock (needed for cache init)
    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.db_path = _db_path
    app.state.golden_repo_manager.get_wiki_enabled.return_value = False
    app.state.golden_repo_manager.golden_repos_dir = tempfile.mkdtemp(suffix="-golden")
    (Path(app.state.golden_repo_manager.golden_repos_dir) / "aliases").mkdir(
        parents=True, exist_ok=True
    )

    # Access filtering service mock
    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = is_admin
    app.state.access_filtering_service.get_accessible_repos.return_value = set()

    # Activated repo manager mock
    activated_mgr = MagicMock()
    activated_mgr.get_wiki_enabled.return_value = wiki_enabled
    activated_mgr.get_activated_repo_path.return_value = repo_path or "/nonexistent/path"
    app.state.activated_repo_manager = activated_mgr

    return app


# ===========================================================================
# Section 1: ActivatedRepoManager wiki methods
# ===========================================================================


class TestActivatedRepoManagerWikiEnabled:
    """Tests for get_wiki_enabled / set_wiki_enabled."""

    def test_get_wiki_enabled_returns_false_by_default(self, tmp_path):
        """Metadata without wiki_enabled key should return False."""
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        user_dir = str(tmp_path / "activated-repos" / "alice")
        _write_metadata(user_dir, "my-repo")  # no wiki_enabled key

        result = manager.get_wiki_enabled("alice", "my-repo")
        assert result is False

    def test_set_wiki_enabled_persists_to_metadata(self, tmp_path):
        """set_wiki_enabled(True) should be readable back as True."""
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        user_dir = str(tmp_path / "activated-repos" / "alice")
        _write_metadata(user_dir, "my-repo")

        manager.set_wiki_enabled("alice", "my-repo", True)
        assert manager.get_wiki_enabled("alice", "my-repo") is True

    def test_set_wiki_enabled_false_persists(self, tmp_path):
        """set_wiki_enabled(False) after True should persist False."""
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        user_dir = str(tmp_path / "activated-repos" / "alice")
        _write_metadata(user_dir, "my-repo", extra={"wiki_enabled": True})

        manager.set_wiki_enabled("alice", "my-repo", False)
        assert manager.get_wiki_enabled("alice", "my-repo") is False

    def test_set_wiki_enabled_raises_for_nonexistent_repo(self, tmp_path):
        """set_wiki_enabled on missing repo should raise ActivatedRepoError."""
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        with pytest.raises(ActivatedRepoError):
            manager.set_wiki_enabled("alice", "nonexistent", True)

    def test_get_wiki_enabled_returns_false_for_nonexistent_repo(self, tmp_path):
        """get_wiki_enabled on missing repo should return False (no exception)."""
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        result = manager.get_wiki_enabled("alice", "nonexistent")
        assert result is False

    def test_set_wiki_enabled_preserves_other_metadata_fields(self, tmp_path):
        """set_wiki_enabled must not overwrite other metadata fields."""
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        user_dir = str(tmp_path / "activated-repos" / "alice")
        _write_metadata(user_dir, "my-repo", extra={"current_branch": "feature-x"})

        manager.set_wiki_enabled("alice", "my-repo", True)

        metadata_path = os.path.join(user_dir, "my-repo_metadata.json")
        with open(metadata_path) as f:
            stored = json.load(f)
        assert stored["current_branch"] == "feature-x"
        assert stored["wiki_enabled"] is True


# ===========================================================================
# Section 2: User wiki route access control
# ===========================================================================


class TestUserWikiAccessControl:
    """Access control: owner + admin can access, others get 404."""

    def test_user_wiki_accessible_by_owner(self, tmp_path):
        """Repo owner gets 200 when wiki is enabled."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "home.md").write_text("# Home")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 200

    def test_user_wiki_accessible_by_admin(self, tmp_path):
        """Admin user gets 200 even when not the owner."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "home.md").write_text("# Home")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="admin",
            is_admin=True,
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 200

    def test_user_wiki_returns_404_for_other_user(self, tmp_path):
        """Non-owner, non-admin user gets 404 (invisible repo pattern)."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "home.md").write_text("# Home")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="bob",
            is_admin=False,
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 404

    def test_user_wiki_returns_404_when_wiki_disabled(self, tmp_path):
        """Owner gets 404 when wiki is disabled."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=False,
            viewer_username="alice",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 404

    def test_user_wiki_returns_404_for_nonexistent_repo(self, tmp_path):
        """404 when the activated repo directory does not exist."""
        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path="/nonexistent/path/that/does/not/exist",
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 404


# ===========================================================================
# Section 3: User wiki content served from activated repo path
# ===========================================================================


class TestUserWikiContent:
    """Content served from user's activated repo directory."""

    def test_user_wiki_root_serves_home_md(self, tmp_path):
        """Wiki root should render home.md from the user's repo."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "home.md").write_text("# My Home\nWelcome to my wiki")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 200
        assert "My Home" in resp.text

    def test_user_wiki_serves_content_from_activated_repo_path(self, tmp_path):
        """Article content should come from the user's activated repo, not golden repo."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "article.md").write_text("# Article\nUser-specific content here")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/article")
        assert resp.status_code == 200
        assert "User-specific content here" in resp.text

    def test_user_wiki_builds_own_sidebar(self, tmp_path):
        """Sidebar TOC should be built from the user's repo content."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "home.md").write_text("# Home")
        (Path(repo_path) / "guide.md").write_text("# Guide")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/")
        assert resp.status_code == 200
        # Sidebar is rendered in the HTML (guide.md should appear in nav)
        assert "guide" in resp.text.lower() or "Guide" in resp.text

    def test_user_wiki_home_link_points_to_user_wiki(self, tmp_path):
        """The Wiki Home toolbar link must point to /wiki/u/{username}/{alias}/."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "article.md").write_text("# Article\nContent")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/article")
        assert resp.status_code == 200
        assert "/wiki/u/alice/my-repo/" in resp.text


# ===========================================================================
# Section 4: WikiCache isolation
# ===========================================================================


class TestUserWikiCacheIsolation:
    """Cache keys for user wikis must be isolated from golden repo cache."""

    def test_user_wiki_cache_key_includes_username(self, tmp_path):
        """WikiCache entries for user wiki use 'u:{username}:{alias}' as repo_alias."""
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        cache = WikiCache(db_path)
        cache.ensure_tables()

        md_file = tmp_path / "article.md"
        md_file.write_text("# Test")

        cache_key = "u:alice:my-repo"
        cache.put_article(cache_key, "article", "<h1>Test</h1>", "Test", md_file)
        result = cache.get_article(cache_key, "article", md_file)
        assert result is not None
        assert result["html"] == "<h1>Test</h1>"

    def test_user_wiki_cache_does_not_pollute_golden_cache(self, tmp_path):
        """User wiki cache key must not match golden repo cache key."""
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        cache = WikiCache(db_path)
        cache.ensure_tables()

        md_file = tmp_path / "article.md"
        md_file.write_text("# Test")

        # Write to user wiki cache
        user_key = "u:alice:my-repo"
        cache.put_article(user_key, "article", "<h1>User</h1>", "User", md_file)

        # Golden repo key should have no entry
        golden_key = "my-repo"
        result = cache.get_article(golden_key, "article", md_file)
        assert result is None

    def test_invalidate_user_wiki_only_affects_user_cache(self, tmp_path):
        """invalidate_user_wiki() should only remove user wiki entries."""
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        cache = WikiCache(db_path)
        cache.ensure_tables()

        md_file = tmp_path / "article.md"
        md_file.write_text("# Test")

        user_key = "u:alice:my-repo"
        golden_key = "my-repo"

        cache.put_article(user_key, "article", "<h1>User</h1>", "User", md_file)
        cache.put_article(golden_key, "article", "<h1>Golden</h1>", "Golden", md_file)

        # Invalidate only user wiki
        cache.invalidate_user_wiki("alice", "my-repo")

        # User cache gone
        assert cache.get_article(user_key, "article", md_file) is None
        # Golden cache intact
        golden_result = cache.get_article(golden_key, "article", md_file)
        assert golden_result is not None
        assert golden_result["html"] == "<h1>Golden</h1>"


# ===========================================================================
# Section 5: Web UI toggle endpoint
# ===========================================================================


class TestUserWikiWebToggle:
    """The /admin/activated-repos/{username}/{alias}/wiki-toggle POST endpoint."""

    def test_wiki_toggle_endpoint_exists_in_web_routes(self):
        """The toggle route must be registered in web_router."""
        from code_indexer.server.web.routes import web_router
        paths = [r.path for r in web_router.routes]
        assert any("activated-repos" in p and "wiki-toggle" in p for p in paths), (
            "Expected an activated-repos wiki-toggle route in web_router paths"
        )

    def test_repos_list_template_has_wiki_checkbox(self):
        """repos_list.html template must contain a user-wiki-toggle checkbox."""
        template_path = (
            Path(__file__).parent
            / "../../../../src/code_indexer/server/web/templates/partials/repos_list.html"
        )
        content = template_path.read_text()
        assert "user-wiki-toggle" in content or "wiki-toggle" in content, (
            "Expected wiki toggle checkbox in repos_list.html template"
        )


# ===========================================================================
# Section 6: Asset route for user wiki
# ===========================================================================


class TestUserWikiAssetRoute:
    """Asset serving from user's activated repo."""

    def test_user_wiki_asset_route_serves_files(self, tmp_path):
        """PNG asset in user's activated repo should be served via asset route."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        # Write a minimal PNG header (8 bytes)
        (Path(repo_path) / "image.png").write_bytes(
            b"\x89PNG\r\n\x1a\n"
        )

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app)
        resp = client.get("/wiki/u/alice/my-repo/_assets/image.png")
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("image/png")


# ===========================================================================
# Section 7: Search route for user wiki
# ===========================================================================


class TestUserWikiSearchRoute:
    """Search endpoint for user wiki."""

    def test_user_wiki_search_endpoint_exists(self, tmp_path):
        """GET /wiki/u/{username}/{alias}/_search should return JSON, not 404/405."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()
        (Path(repo_path) / "home.md").write_text("# Home")

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/u/alice/my-repo/_search?q=test")
        # Should return JSON (200), not 404 or 405
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("application/json")

    def test_user_wiki_search_returns_empty_for_short_query(self, tmp_path):
        """Search with query < 2 chars should return empty list."""
        repo_path = str(tmp_path / "repo")
        Path(repo_path).mkdir()

        app = _make_user_wiki_app(
            username="alice",
            alias="my-repo",
            repo_path=repo_path,
            wiki_enabled=True,
            viewer_username="alice",
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/wiki/u/alice/my-repo/_search?q=x")
        assert resp.status_code == 200
        assert resp.json() == []
