"""Tests for WikiCache SQLite render cache (Story #283)."""
import os
import sqlite3
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from code_indexer.server.wiki.wiki_cache import WikiCache


@pytest.fixture
def cache_db():
    """Provide a temp SQLite db path for each test."""
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
    """Provide a temp directory for repo files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestEnsureTables:
    def test_ensure_tables_creates_wiki_cache_table(self, cache_db):
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "wiki_cache" in tables

    def test_ensure_tables_creates_wiki_sidebar_cache_table(self, cache_db):
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "wiki_sidebar_cache" in tables

    def test_wiki_cache_table_has_correct_columns(self, cache_db):
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_cache)").fetchall()}
        conn.close()
        expected = {"repo_alias", "article_path", "rendered_html", "title", "file_mtime", "file_size", "rendered_at"}
        assert expected.issubset(cols)

    def test_wiki_sidebar_cache_table_has_correct_columns(self, cache_db):
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_sidebar_cache)").fetchall()}
        conn.close()
        expected = {"repo_alias", "sidebar_json", "max_mtime", "built_at"}
        assert expected.issubset(cols)

    def test_ensure_tables_idempotent(self, cache_db):
        """Calling ensure_tables() twice should not raise."""
        cache, _ = cache_db
        cache.ensure_tables()  # second call, should be no-op


class TestPutGetArticle:
    def test_put_article_stores_data(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "test.md"
        f.write_text("# Test")
        cache.put_article("repo1", "test", "<h1>Test</h1>", "Test", f)
        result = cache.get_article("repo1", "test", f)
        assert result is not None
        assert result["html"] == "<h1>Test</h1>"
        assert result["title"] == "Test"

    def test_get_article_returns_cached_on_stat_match(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "article.md"
        f.write_text("Content")
        cache.put_article("repo1", "article", "<p>Content</p>", "Article", f)
        result = cache.get_article("repo1", "article", f)
        assert result is not None

    def test_get_article_returns_none_on_mtime_change(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "article.md"
        f.write_text("Original")
        cache.put_article("repo1", "article", "<p>Original</p>", "Article", f)
        # Modify file and update mtime
        time.sleep(0.01)
        f.write_text("Modified")
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 1.0))
        result = cache.get_article("repo1", "article", f)
        assert result is None

    def test_get_article_returns_none_on_size_change(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "article.md"
        f.write_text("Short")
        cache.put_article("repo1", "article", "<p>Short</p>", "Article", f)
        # Write longer content (same mtime to isolate size check)
        original_mtime = f.stat().st_mtime
        f.write_text("Much longer content that is definitely bigger")
        os.utime(f, (original_mtime, original_mtime))
        result = cache.get_article("repo1", "article", f)
        assert result is None

    def test_get_article_returns_none_for_uncached(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "missing.md"
        f.write_text("Content")
        result = cache.get_article("repo1", "nonexistent", f)
        assert result is None

    def test_put_article_overwrites_existing(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "article.md"
        f.write_text("V1")
        cache.put_article("repo1", "article", "<p>V1</p>", "Version 1", f)
        f.write_text("V2")
        cache.put_article("repo1", "article", "<p>V2</p>", "Version 2", f)
        result = cache.get_article("repo1", "article", f)
        assert result is not None
        assert result["html"] == "<p>V2</p>"

    def test_put_article_with_datetime_metadata_does_not_raise(self, cache_db, repo_dir):
        """Story #289: put_article must not raise TypeError when metadata contains datetime/date objects.

        The python-frontmatter library parses YAML date fields as Python datetime/date objects.
        These must be serialized to ISO strings before JSON storage.
        """
        cache, _ = cache_db
        f = repo_dir / "dated-article.md"
        f.write_text("# Dated Article")
        metadata_with_dates = {
            "title": "Dated Article",
            "created": date(2024, 1, 15),
            "modified": datetime(2024, 6, 20, 10, 30, 0),
            "visibility": "internal",
        }
        # Must not raise TypeError: Object of type datetime is not JSON serializable
        cache.put_article("repo1", "dated-article", "<h1>Dated Article</h1>", "Dated Article", f,
                          metadata=metadata_with_dates)
        result = cache.get_article("repo1", "dated-article", f)
        assert result is not None
        cached_meta = result.get("metadata") or {}
        # Dates should be stored as ISO strings and round-trip correctly
        assert cached_meta.get("created") == "2024-01-15"
        assert cached_meta.get("modified") == "2024-06-20T10:30:00"
        assert cached_meta.get("visibility") == "internal"


class TestPutGetSidebar:
    def test_put_sidebar_stores_json(self, cache_db, repo_dir):
        cache, _ = cache_db
        (repo_dir / "page.md").write_text("# Page")
        sidebar = [{"name": "Root", "articles": [{"title": "Page", "path": "page"}]}]
        cache.put_sidebar("repo1", sidebar, repo_dir)
        result = cache.get_sidebar("repo1", repo_dir)
        assert result is not None
        assert result[0]["name"] == "Root"

    def test_get_sidebar_returns_cached_on_mtime_match(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "page.md"
        f.write_text("# Page")
        sidebar = [{"name": "Root", "articles": []}]
        cache.put_sidebar("repo1", sidebar, repo_dir)
        result = cache.get_sidebar("repo1", repo_dir)
        assert result is not None

    def test_get_sidebar_returns_cached_after_mtime_change(self, cache_db, repo_dir):
        """Story #304: get_sidebar no longer polls filesystem mtime.
        Cache coherence is maintained via event-driven invalidation (WikiCacheInvalidator).
        Adding a new file does NOT invalidate the sidebar cache - explicit invalidate_repo() must be called.
        """
        cache, _ = cache_db
        f = repo_dir / "page.md"
        f.write_text("# Page")
        sidebar = [{"name": "Root", "articles": []}]
        cache.put_sidebar("repo1", sidebar, repo_dir)
        # Add new file to change max mtime
        time.sleep(0.01)
        new_file = repo_dir / "new-page.md"
        new_file.write_text("# New Page")
        os.utime(new_file, (new_file.stat().st_atime, new_file.stat().st_mtime + 1.0))
        # Cache is still valid - mtime change does NOT invalidate (event-driven model)
        result = cache.get_sidebar("repo1", repo_dir)
        assert result is not None
        assert result[0]["name"] == "Root"

    def test_get_sidebar_returns_none_for_uncached(self, cache_db, repo_dir):
        cache, _ = cache_db
        result = cache.get_sidebar("never-stored", repo_dir)
        assert result is None

    def test_get_sidebar_ignores_hidden_dirs(self, cache_db, repo_dir):
        """max_mtime computation must skip hidden directories."""
        cache, _ = cache_db
        f = repo_dir / "page.md"
        f.write_text("# Page")
        sidebar = [{"name": "Root", "articles": []}]
        cache.put_sidebar("repo1", sidebar, repo_dir)
        # Add hidden dir file - should NOT change max_mtime
        hidden = repo_dir / ".git"
        hidden.mkdir()
        hidden_file = hidden / "notes.md"
        hidden_file.write_text("# hidden")
        os.utime(hidden_file, (hidden_file.stat().st_atime, hidden_file.stat().st_mtime + 100.0))
        # Cache should still be valid because hidden dir is ignored
        result = cache.get_sidebar("repo1", repo_dir)
        assert result is not None


class TestInvalidation:
    def test_invalidate_repo_deletes_all_entries(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "article.md"
        f.write_text("Content")
        cache.put_article("repo1", "article", "<p>X</p>", "X", f)
        cache.invalidate_repo("repo1")
        result = cache.get_article("repo1", "article", f)
        assert result is None

    def test_invalidate_repo_does_not_affect_other_repos(self, cache_db, repo_dir):
        cache, _ = cache_db
        f = repo_dir / "article.md"
        f.write_text("Content")
        cache.put_article("repo1", "article", "<p>R1</p>", "R1", f)
        cache.put_article("repo2", "article", "<p>R2</p>", "R2", f)
        cache.invalidate_repo("repo1")
        result = cache.get_article("repo2", "article", f)
        assert result is not None
        assert result["html"] == "<p>R2</p>"

    def test_invalidate_repo_clears_sidebar_too(self, cache_db, repo_dir):
        cache, _ = cache_db
        (repo_dir / "page.md").write_text("# Page")
        sidebar = [{"name": "Root", "articles": []}]
        cache.put_sidebar("repo1", sidebar, repo_dir)
        cache.invalidate_repo("repo1")
        result = cache.get_sidebar("repo1", repo_dir)
        assert result is None


class TestCachePersistence:
    def test_cache_survives_instance_recreation(self, repo_dir):
        """Cache data should persist across WikiCache instantiation."""
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            c1 = WikiCache(db_path)
            c1.ensure_tables()
            f = repo_dir / "article.md"
            f.write_text("Persistent content")
            c1.put_article("repo1", "article", "<p>Persistent</p>", "Title", f)

            c2 = WikiCache(db_path)
            result = c2.get_article("repo1", "article", f)
            assert result is not None
            assert result["html"] == "<p>Persistent</p>"
        finally:
            os.unlink(db_path)
