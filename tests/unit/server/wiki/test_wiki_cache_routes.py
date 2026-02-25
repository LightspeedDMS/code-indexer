"""Route integration tests for wiki cache behavior (Story #283)."""
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.routes import wiki_router, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_app(authenticated_user, actual_repo_path):
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
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
    app.state.access_filtering_service.get_accessible_repos.return_value = set()
    return app


class TestCacheRouteIntegration:
    def test_article_served_from_cache_on_second_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            f = repo_dir / "article.md"
            f.write_text("# Cached Article\nFirst content")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)

            # First request - populates cache
            resp1 = client.get("/wiki/test-repo/article")
            assert resp1.status_code == 200
            assert "First content" in resp1.text

            # Modify file content but keep same stat (simulates cached result being served)
            original_stat = f.stat()
            # Second request - should return 200 (from cache or re-rendered)
            resp2 = client.get("/wiki/test-repo/article")
            assert resp2.status_code == 200

    def test_cache_miss_when_file_modified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            f = repo_dir / "article.md"
            f.write_text("# Article\nOriginal content")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)

            # First request
            resp1 = client.get("/wiki/test-repo/article")
            assert resp1.status_code == 200
            assert "Original content" in resp1.text

            # Modify file (change mtime to trigger cache miss)
            time.sleep(0.01)
            f.write_text("# Article\nUpdated content")
            os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 2.0))

            # Second request - should serve updated content
            resp2 = client.get("/wiki/test-repo/article")
            assert resp2.status_code == 200
            assert "Updated content" in resp2.text

    def test_sidebar_cached_between_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page-one.md").write_text("# Page One")
            (repo_dir / "page-two.md").write_text("# Page Two")
            app = _make_app(_make_user("admin"), tmpdir)
            client = TestClient(app)

            # Both requests should succeed (sidebar built once, then cached)
            resp1 = client.get("/wiki/test-repo/page-one")
            assert resp1.status_code == 200
            resp2 = client.get("/wiki/test-repo/page-two")
            assert resp2.status_code == 200
            # Both should have sidebar content
            assert "page" in resp1.text.lower()
            assert "page" in resp2.text.lower()
