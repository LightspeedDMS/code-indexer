"""Integration tests for wiki cache invalidation (Story #304).

Tests the complete chain from event to cache invalidation using
real WikiCache and WikiCacheInvalidator (no mocking of these components).
"""
import os
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator


@pytest.fixture
def fresh_invalidator():
    """Return a fresh WikiCacheInvalidator (not the singleton) wired to a real cache."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    cache = WikiCache(path)
    cache.ensure_tables()
    invalidator = WikiCacheInvalidator()
    invalidator.set_wiki_cache(cache)
    yield invalidator, cache
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def repo_dir_with_md():
    """Provide a temp directory with .md files for sidebar caching."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        (p / "home.md").write_text("# Home")
        (p / "guide.md").write_text("# Guide")
        (p / "subdir").mkdir()
        (p / "subdir" / "advanced.md").write_text("# Advanced")
        yield p


class TestInvalidationChainEndToEnd:
    """Integration: full chain from event to cache cleared."""

    def test_md_file_change_clears_sidebar_cache(self, fresh_invalidator, repo_dir_with_md):
        """Complete chain: md file change -> invalidator -> wiki_cache cleared (AC1)."""
        invalidator, cache = fresh_invalidator
        sidebar_data = [{"title": "Home", "path": ""}]
        cache.put_sidebar("my-repo", sidebar_data, repo_dir_with_md)

        # Verify cached
        assert cache.get_sidebar("my-repo", repo_dir_with_md) == sidebar_data

        # Trigger file-change event
        invalidator.invalidate_for_file_change("my-repo", "home.md")

        # Cache must be cleared
        assert cache.get_sidebar("my-repo", repo_dir_with_md) is None

    def test_non_md_file_change_does_not_clear_cache(self, fresh_invalidator, repo_dir_with_md):
        """Non-md file change must not affect wiki cache (AC5)."""
        invalidator, cache = fresh_invalidator
        sidebar_data = [{"title": "Home", "path": ""}]
        cache.put_sidebar("my-repo", sidebar_data, repo_dir_with_md)

        invalidator.invalidate_for_file_change("my-repo", "main.go")

        # Cache must still be intact
        assert cache.get_sidebar("my-repo", repo_dir_with_md) == sidebar_data

    def test_git_operation_clears_sidebar_cache(self, fresh_invalidator, repo_dir_with_md):
        """Git operation -> invalidator -> wiki_cache cleared (AC2)."""
        invalidator, cache = fresh_invalidator
        sidebar_data = [{"title": "Home", "path": ""}]
        cache.put_sidebar("my-repo", sidebar_data, repo_dir_with_md)

        invalidator.invalidate_for_git_operation("my-repo")

        assert cache.get_sidebar("my-repo", repo_dir_with_md) is None

    def test_refresh_complete_strips_global_and_clears_cache(
        self, fresh_invalidator, repo_dir_with_md
    ):
        """on_refresh_complete strips -global suffix and clears the correct repo cache (AC3)."""
        invalidator, cache = fresh_invalidator
        sidebar_data = [{"title": "Home", "path": ""}]
        # Store under the non-global alias (how wiki routes store it)
        cache.put_sidebar("my-repo", sidebar_data, repo_dir_with_md)
        # Also store a different repo's cache that must NOT be cleared
        cache.put_sidebar("other-repo", [{"title": "Other"}], repo_dir_with_md)

        # Simulate refresh complete event (uses -global suffix)
        invalidator.on_refresh_complete("my-repo-global")

        # my-repo cache cleared
        assert cache.get_sidebar("my-repo", repo_dir_with_md) is None
        # other-repo cache intact
        assert cache.get_sidebar("other-repo", repo_dir_with_md) is not None

    def test_article_cache_also_cleared_on_invalidation(self, fresh_invalidator, repo_dir_with_md):
        """invalidate_repo clears both sidebar AND article cache."""
        invalidator, cache = fresh_invalidator
        # Put sidebar
        cache.put_sidebar("my-repo", [{"title": "Home"}], repo_dir_with_md)
        # Put article
        home_md = repo_dir_with_md / "home.md"
        cache.put_article("my-repo", "", "<h1>Home</h1>", "Home", home_md)

        # Trigger invalidation
        invalidator.invalidate_repo("my-repo")

        # Both should be gone
        assert cache.get_sidebar("my-repo", repo_dir_with_md) is None
        assert cache.get_article("my-repo", "", home_md) is None

    def test_invalidation_does_not_affect_other_repos(self, fresh_invalidator, repo_dir_with_md):
        """Invalidating one repo must not affect other repos' caches."""
        invalidator, cache = fresh_invalidator
        cache.put_sidebar("repo-a", [{"title": "A"}], repo_dir_with_md)
        cache.put_sidebar("repo-b", [{"title": "B"}], repo_dir_with_md)

        invalidator.invalidate_repo("repo-a")

        assert cache.get_sidebar("repo-a", repo_dir_with_md) is None
        assert cache.get_sidebar("repo-b", repo_dir_with_md) is not None

    def test_cold_cache_still_returns_none_then_caches_correctly(
        self, fresh_invalidator, repo_dir_with_md
    ):
        """AC6: Cold cache still works — None on miss, data on hit after put (AC6)."""
        invalidator, cache = fresh_invalidator

        # Cold cache: miss
        assert cache.get_sidebar("new-repo", repo_dir_with_md) is None

        # Populate
        sidebar_data = [{"title": "Page", "path": "guide"}]
        cache.put_sidebar("new-repo", sidebar_data, repo_dir_with_md)

        # Now hit
        assert cache.get_sidebar("new-repo", repo_dir_with_md) == sidebar_data

    def test_invalidator_noop_before_wiring(self):
        """Invalidator is safe to call before wiki_cache is set (AC8 - never blocks)."""
        invalidator = WikiCacheInvalidator()
        # None of these should raise
        invalidator.invalidate_repo("any-repo")
        invalidator.invalidate_for_file_change("any-repo", "file.md")
        invalidator.invalidate_for_git_operation("any-repo")
        invalidator.on_refresh_complete("any-repo-global")

    def test_user_wiki_cache_key_format(self, fresh_invalidator, repo_dir_with_md):
        """User wiki caches use 'u:username:alias' key format (AC7)."""
        invalidator, cache = fresh_invalidator
        user_cache_key = "u:alice:my-repo"
        cache.put_sidebar(user_cache_key, [{"title": "Home"}], repo_dir_with_md)

        # Direct repo invalidation with user key
        invalidator.invalidate_repo(user_cache_key)

        assert cache.get_sidebar(user_cache_key, repo_dir_with_md) is None
