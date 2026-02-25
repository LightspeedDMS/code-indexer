"""Tests for wiki article metadata display panel (Story #289).

RED phase: all tests written before implementation exists.
Covers:
 - WikiService.format_date_human_readable()
 - WikiService.prepare_metadata_context()
 - WikiCache metadata_json storage/retrieval
 - Route integration: metadata panel rendered in article.html
"""
import json
import os
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.wiki.wiki_service import WikiService
from code_indexer.server.wiki.routes import wiki_router, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_db():
    """Temp SQLite db with tables created."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = WikiCache(path)
    c.ensure_tables()
    yield c, path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def repo_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_app(actual_repo_path, db_path, authenticated_user=None):
    """Build a FastAPI test app with wiki router and minimal app state."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    if authenticated_user:
        app.dependency_overrides[get_current_user_hybrid] = lambda: authenticated_user

    app.include_router(wiki_router, prefix="/wiki")

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = True
    app.state.golden_repo_manager.db_path = db_path

    golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test-meta"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
    app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    app.state.access_filtering_service.get_accessible_repos.return_value = {"test-repo"}
    return app


# ---------------------------------------------------------------------------
# Unit: WikiService.format_date_human_readable
# ---------------------------------------------------------------------------


class TestFormatDateHumanReadable:
    """Tests for WikiService.format_date_human_readable()."""

    def setup_method(self):
        self.service = WikiService()

    def test_formats_iso_date_string(self):
        """ISO 8601 date string '2024-03-15' -> 'March 15, 2024'."""
        result = self.service.format_date_human_readable("2024-03-15")
        assert result == "March 15, 2024"

    def test_formats_iso_datetime_string(self):
        """ISO datetime string '2024-03-15T10:30:00' -> 'March 15, 2024'."""
        result = self.service.format_date_human_readable("2024-03-15T10:30:00")
        assert result == "March 15, 2024"

    def test_formats_datetime_object(self):
        """datetime object -> 'Month DD, YYYY'."""
        dt = datetime(2024, 3, 15, 10, 30, 0)
        result = self.service.format_date_human_readable(dt)
        assert result == "March 15, 2024"

    def test_formats_date_object(self):
        """date object -> 'Month DD, YYYY'."""
        d = date(2024, 3, 15)
        result = self.service.format_date_human_readable(d)
        assert result == "March 15, 2024"

    def test_invalid_input_returns_none(self):
        """Non-parseable input returns None (no exception raised)."""
        result = self.service.format_date_human_readable("not-a-date")
        assert result is None

    def test_none_input_returns_none(self):
        """None input returns None."""
        result = self.service.format_date_human_readable(None)
        assert result is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        result = self.service.format_date_human_readable("")
        assert result is None

    def test_formats_single_digit_day(self):
        """Date string '2024-01-07' -> 'January 7, 2024' (single-digit day)."""
        result = self.service.format_date_human_readable("2024-01-07")
        assert result == "January 7, 2024"


# ---------------------------------------------------------------------------
# Unit: WikiService.prepare_metadata_context
# ---------------------------------------------------------------------------


class TestPrepareMetadataContext:
    """Tests for WikiService.prepare_metadata_context()."""

    def setup_method(self):
        self.service = WikiService()

    def _make_cache(self, view_count=0):
        """Return a mock WikiCache that returns the given view count."""
        cache = MagicMock()
        cache.get_view_count.return_value = view_count
        return cache

    def test_returns_real_views_when_greater_than_zero(self):
        """real_views > 0 -> context includes 'real_views' key."""
        cache = self._make_cache(view_count=42)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert ctx.get("real_views") == 42

    def test_omits_real_views_when_zero(self):
        """real_views == 0 -> 'real_views' key not in context."""
        cache = self._make_cache(view_count=0)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert "real_views" not in ctx

    def test_includes_created_date_when_present(self):
        """metadata with 'created' field -> context has formatted 'created'."""
        cache = self._make_cache(view_count=0)
        metadata = {"created": "2024-03-15"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("created") == "March 15, 2024"

    def test_omits_created_when_missing(self):
        """metadata without 'created' -> 'created' not in context."""
        cache = self._make_cache(view_count=0)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert "created" not in ctx

    def test_includes_modified_date_when_present(self):
        """metadata with 'modified' -> context has formatted 'modified'."""
        cache = self._make_cache(view_count=0)
        metadata = {"modified": "2024-06-01"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("modified") == "June 1, 2024"

    def test_modified_also_reads_updated_key(self):
        """metadata with 'updated' (synonym for modified) -> context has 'modified'."""
        cache = self._make_cache(view_count=0)
        metadata = {"updated": "2024-06-01"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("modified") == "June 1, 2024"

    def test_omits_modified_when_missing(self):
        """metadata without 'modified' or 'updated' -> 'modified' not in context."""
        cache = self._make_cache(view_count=0)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert "modified" not in ctx

    def test_includes_visibility_when_present(self):
        """metadata with 'visibility' -> context has 'visibility' and 'visibility_class'."""
        cache = self._make_cache(view_count=0)
        metadata = {"visibility": "public"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("visibility") == "public"
        assert "visibility_class" in ctx

    def test_draft_true_overrides_visibility(self):
        """metadata with draft=True -> visibility becomes 'draft' regardless of visibility field."""
        cache = self._make_cache(view_count=0)
        metadata = {"visibility": "public", "draft": True}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("visibility") == "draft"

    def test_omits_visibility_when_missing(self):
        """metadata without visibility/draft/status -> 'visibility' not in context."""
        cache = self._make_cache(view_count=0)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert "visibility" not in ctx
        assert "visibility_class" not in ctx

    def test_visibility_class_published_contains_published(self):
        """visibility='published' -> visibility_class contains 'published'."""
        cache = self._make_cache(view_count=0)
        metadata = {"visibility": "published"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert "published" in ctx.get("visibility_class", "")

    def test_visibility_class_internal_contains_internal(self):
        """visibility='internal' -> visibility_class contains 'internal'."""
        cache = self._make_cache(view_count=0)
        metadata = {"visibility": "internal"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert "internal" in ctx.get("visibility_class", "")

    def test_visibility_class_draft_contains_draft(self):
        """draft=True -> visibility_class contains 'draft'."""
        cache = self._make_cache(view_count=0)
        metadata = {"draft": True}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert "draft" in ctx.get("visibility_class", "")

    def test_includes_category_when_present(self):
        """metadata with 'category' -> context has 'category'."""
        cache = self._make_cache(view_count=0)
        metadata = {"category": "Operations"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("category") == "Operations"

    def test_omits_category_when_missing(self):
        """metadata without 'category' -> 'category' not in context."""
        cache = self._make_cache(view_count=0)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert "category" not in ctx

    def test_empty_context_when_all_fields_missing_and_zero_views(self):
        """No metadata and 0 views -> empty context dict."""
        cache = self._make_cache(view_count=0)
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert ctx == {}

    def test_full_metadata_produces_all_fields(self):
        """All metadata fields present + views -> all context fields populated."""
        cache = self._make_cache(view_count=15)
        metadata = {
            "created": "2024-01-01",
            "modified": "2024-06-15",
            "visibility": "public",
            "category": "Guides",
        }
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert "real_views" in ctx
        assert "created" in ctx
        assert "modified" in ctx
        assert "visibility" in ctx
        assert "visibility_class" in ctx
        assert "category" in ctx


# ---------------------------------------------------------------------------
# Unit: WikiCache metadata_json column
# ---------------------------------------------------------------------------


class TestWikiCacheMetadataJson:
    """Tests for metadata_json storage in wiki_cache table."""

    def test_wiki_cache_table_has_metadata_json_column(self, cache_db):
        """wiki_cache table must have a metadata_json column after ensure_tables()."""
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(wiki_cache)").fetchall()
        }
        conn.close()
        assert "metadata_json" in cols

    def test_put_article_stores_metadata_json(self, cache_db, repo_dir):
        """put_article() with metadata stores metadata_json in the DB row."""
        cache, db_path = cache_db
        md_file = repo_dir / "article.md"
        md_file.write_text("# Article\nContent")
        metadata = {"created": "2024-01-01", "visibility": "public"}

        cache.put_article("repo1", "article", "<p>Content</p>", "Article", md_file, metadata=metadata)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT metadata_json FROM wiki_cache WHERE repo_alias=? AND article_path=?",
            ("repo1", "article"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is not None
        stored = json.loads(row[0])
        assert stored.get("created") == "2024-01-01"
        assert stored.get("visibility") == "public"

    def test_put_article_stores_null_metadata_when_not_provided(self, cache_db, repo_dir):
        """put_article() without metadata arg stores NULL metadata_json."""
        cache, db_path = cache_db
        md_file = repo_dir / "article.md"
        md_file.write_text("# Article\nContent")

        cache.put_article("repo1", "article", "<p>Content</p>", "Article", md_file)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT metadata_json FROM wiki_cache WHERE repo_alias=? AND article_path=?",
            ("repo1", "article"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is None or row[0] == ""

    def test_get_article_returns_metadata_when_stored(self, cache_db, repo_dir):
        """get_article() returns dict with 'metadata' key when metadata_json is stored."""
        cache, _ = cache_db
        md_file = repo_dir / "article.md"
        md_file.write_text("# Article\nContent")
        metadata = {"created": "2024-03-15"}

        cache.put_article("repo1", "article", "<p>Content</p>", "Article", md_file, metadata=metadata)
        result = cache.get_article("repo1", "article", md_file)

        assert result is not None
        assert "metadata" in result
        assert result["metadata"].get("created") == "2024-03-15"

    def test_get_article_returns_none_metadata_when_not_stored(self, cache_db, repo_dir):
        """get_article() returns dict with metadata=None when no metadata was stored."""
        cache, _ = cache_db
        md_file = repo_dir / "article.md"
        md_file.write_text("# Article\nContent")

        cache.put_article("repo1", "article", "<p>Content</p>", "Article", md_file)
        result = cache.get_article("repo1", "article", md_file)

        assert result is not None
        assert result.get("metadata") is None or result.get("metadata") == {}


# ---------------------------------------------------------------------------
# Route integration: metadata panel rendered in article HTML
# ---------------------------------------------------------------------------


class TestMetadataPanelRouteIntegration:
    """Integration tests verifying the metadata panel appears in rendered article HTML."""

    def test_article_page_shows_metadata_panel_with_front_matter(self):
        """Article with front matter metadata -> rendered HTML contains wiki-metadata-panel."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                md_content = (
                    "---\n"
                    "title: Test Article\n"
                    "created: '2024-03-15'\n"
                    "visibility: public\n"
                    "---\n"
                    "# Test Article\n"
                    "Content here."
                )
                (repo_dir / "article.md").write_text(md_content)
                app = _make_app(str(repo_dir), db_path, authenticated_user=_make_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/article")
                assert resp.status_code == 200
                assert "wiki-metadata-panel" in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_article_page_shows_real_views_from_db(self):
        """real_views shown in metadata panel comes from DB, not front matter 'views' field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                # front matter has views=999 but DB will have the real count
                md_content = (
                    "---\n"
                    "title: Popular\n"
                    "views: 999\n"
                    "---\n"
                    "# Popular\nContent."
                )
                (repo_dir / "popular.md").write_text(md_content)

                # Pre-seed DB with 7 real views
                cache = WikiCache(db_path)
                cache.ensure_tables()
                for _ in range(7):
                    cache.increment_view("test-repo", "popular")

                app = _make_app(str(repo_dir), db_path, authenticated_user=_make_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/popular")
                assert resp.status_code == 200
                # The metadata panel must show "Views:" (DB-driven)
                assert "Views:" in resp.text
                # The front matter raw value 999 must NOT appear as the view count
                assert "Views: 999" not in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_article_page_shows_views_only_when_no_front_matter(self):
        """Article with no front matter -> metadata panel shows Views: 1 (route increments view before render)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                (repo_dir / "plain.md").write_text("# Plain\nNo front matter.")
                app = _make_app(str(repo_dir), db_path, authenticated_user=_make_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/plain")
                assert resp.status_code == 200
                # Route increments view before rendering, so real_views >= 1 -> panel is shown
                assert "wiki-metadata-panel" in resp.text
                assert "Views: 1" in resp.text
                # No front matter fields should be present
                assert "Created:" not in resp.text
                assert "Modified:" not in resp.text
                assert "metadata-badge" not in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_cached_article_still_shows_metadata_panel(self):
        """Second request (served from cache) also shows the metadata panel."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                md_content = (
                    "---\n"
                    "title: Cached\n"
                    "created: '2024-05-10'\n"
                    "---\n"
                    "# Cached\nContent."
                )
                (repo_dir / "cached.md").write_text(md_content)
                app = _make_app(str(repo_dir), db_path, authenticated_user=_make_user("alice"))
                client = TestClient(app)

                # First request populates cache
                resp1 = client.get("/wiki/test-repo/cached")
                assert resp1.status_code == 200

                # Second request served from cache must also show metadata panel
                resp2 = client.get("/wiki/test-repo/cached")
                assert resp2.status_code == 200
                assert "wiki-metadata-panel" in resp2.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_article_page_shows_created_date_formatted(self):
        """Article with 'created' front matter -> 'March 15, 2024' visible in HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                md_content = (
                    "---\n"
                    "title: Dated\n"
                    "created: '2024-03-15'\n"
                    "---\n"
                    "# Dated\nContent."
                )
                (repo_dir / "dated.md").write_text(md_content)
                app = _make_app(str(repo_dir), db_path, authenticated_user=_make_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/dated")
                assert resp.status_code == 200
                assert "March 15, 2024" in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_article_page_shows_visibility_badge(self):
        """Article with visibility='public' -> badge with 'public' class visible in HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                md_content = (
                    "---\n"
                    "title: Public Article\n"
                    "visibility: public\n"
                    "---\n"
                    "# Public\nContent."
                )
                (repo_dir / "pub.md").write_text(md_content)
                app = _make_app(str(repo_dir), db_path, authenticated_user=_make_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/pub")
                assert resp.status_code == 200
                assert "metadata-badge" in resp.text
                assert "public" in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass
