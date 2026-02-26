"""Tests that get_sidebar no longer polls filesystem (Story #304, AC4).

AC4: Sidebar cache serves without filesystem poll on cache hit (no rglob in hot path).
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.wiki.wiki_cache import WikiCache, _max_mtime_of_md_files


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
    """Provide a temp directory with a markdown file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "page.md").write_text("# Hello")
        yield Path(tmpdir)


class TestGetSidebarNoPoll:
    """AC4: get_sidebar must NOT call _max_mtime_of_md_files on cache hit."""

    def test_get_sidebar_cache_hit_does_not_call_max_mtime(self, cache_db, repo_dir):
        """On a cache hit, get_sidebar must return cached data without filesystem poll."""
        cache, _ = cache_db
        sidebar_data = [{"title": "Page", "path": "page"}]
        cache.put_sidebar("my-repo", sidebar_data, repo_dir)

        with patch(
            "code_indexer.server.wiki.wiki_cache._max_mtime_of_md_files"
        ) as mock_mtime:
            result = cache.get_sidebar("my-repo", repo_dir)

        # Must NOT have called _max_mtime_of_md_files on the cache hit path
        mock_mtime.assert_not_called()
        assert result == sidebar_data

    def test_get_sidebar_cache_miss_returns_none(self, cache_db, repo_dir):
        """get_sidebar returns None when no row exists in the cache."""
        cache, _ = cache_db
        result = cache.get_sidebar("nonexistent-repo", repo_dir)
        assert result is None

    def test_get_sidebar_returns_correct_data_on_hit(self, cache_db, repo_dir):
        """get_sidebar returns exactly the sidebar data that was stored."""
        cache, _ = cache_db
        sidebar_data = [
            {"title": "Intro", "path": "intro"},
            {"title": "Guide", "path": "guide"},
        ]
        cache.put_sidebar("test-repo", sidebar_data, repo_dir)
        result = cache.get_sidebar("test-repo", repo_dir)
        assert result == sidebar_data

    def test_get_sidebar_after_invalidate_returns_none(self, cache_db, repo_dir):
        """After invalidate_repo, get_sidebar must return None (cache miss)."""
        cache, _ = cache_db
        sidebar_data = [{"title": "Page", "path": "page"}]
        cache.put_sidebar("my-repo", sidebar_data, repo_dir)
        cache.invalidate_repo("my-repo")
        result = cache.get_sidebar("my-repo", repo_dir)
        assert result is None
