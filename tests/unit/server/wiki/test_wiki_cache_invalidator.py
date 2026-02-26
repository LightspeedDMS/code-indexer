"""Tests for WikiCacheInvalidator service (Story #304)."""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.wiki.wiki_cache import WikiCache


class TestWikiCacheInvalidatorSetWikiCache:
    """Tests for set_wiki_cache wiring."""

    def test_set_wiki_cache_stores_reference(self):
        """set_wiki_cache must store the provided WikiCache instance."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        assert invalidator.wiki_cache is mock_cache

    def test_wiki_cache_is_none_by_default(self):
        """WikiCacheInvalidator.wiki_cache must be None before set_wiki_cache is called."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        assert invalidator.wiki_cache is None


class TestWikiCacheInvalidatorInvalidateRepo:
    """Tests for invalidate_repo method."""

    def test_invalidate_repo_calls_wiki_cache_invalidate_repo(self):
        """invalidate_repo must delegate to wiki_cache.invalidate_repo."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_repo("my-repo")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")

    def test_invalidate_repo_noop_when_no_cache(self):
        """invalidate_repo must be a no-op when wiki_cache is None (never raises)."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        # Must not raise
        invalidator.invalidate_repo("my-repo")


class TestWikiCacheInvalidatorForFileChange:
    """Tests for invalidate_for_file_change method."""

    def test_md_file_triggers_invalidation(self):
        """A .md file change must trigger wiki cache invalidation."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_for_file_change("my-repo", "docs/guide.md")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")

    def test_markdown_file_triggers_invalidation(self):
        """A .markdown file change must trigger wiki cache invalidation."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_for_file_change("my-repo", "docs/readme.markdown")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")

    def test_txt_file_triggers_invalidation(self):
        """A .txt file change must trigger wiki cache invalidation."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_for_file_change("my-repo", "notes.txt")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")

    def test_non_md_file_does_not_trigger_invalidation(self):
        """A .py file change must NOT trigger wiki cache invalidation (AC5)."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_for_file_change("my-repo", "src/main.py")
        mock_cache.invalidate_repo.assert_not_called()

    def test_json_file_does_not_trigger_invalidation(self):
        """A .json file change must NOT trigger wiki cache invalidation (AC5)."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_for_file_change("my-repo", "config.json")
        mock_cache.invalidate_repo.assert_not_called()

    def test_file_change_noop_when_no_cache(self):
        """invalidate_for_file_change must be a no-op when wiki_cache is None (never raises)."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        # Must not raise even for md files
        invalidator.invalidate_for_file_change("my-repo", "docs/guide.md")


class TestWikiCacheInvalidatorForGitOperation:
    """Tests for invalidate_for_git_operation method."""

    def test_git_operation_triggers_invalidation(self):
        """invalidate_for_git_operation must trigger wiki cache invalidation."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.invalidate_for_git_operation("my-repo")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")

    def test_git_operation_noop_when_no_cache(self):
        """invalidate_for_git_operation must be a no-op when wiki_cache is None."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        # Must not raise
        invalidator.invalidate_for_git_operation("my-repo")


class TestWikiCacheInvalidatorOnRefreshComplete:
    """Tests for on_refresh_complete method."""

    def test_on_refresh_complete_strips_global_suffix(self):
        """on_refresh_complete must strip -global suffix before invalidating (AC3)."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.on_refresh_complete("my-repo-global")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")

    def test_on_refresh_complete_no_suffix_uses_alias_as_is(self):
        """on_refresh_complete with no -global suffix must use alias_name as-is."""
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        invalidator = WikiCacheInvalidator()
        mock_cache = MagicMock(spec=WikiCache)
        invalidator.set_wiki_cache(mock_cache)
        invalidator.on_refresh_complete("my-repo")
        mock_cache.invalidate_repo.assert_called_once_with("my-repo")


class TestWikiCacheInvalidatorSingleton:
    """Tests for module-level singleton."""

    def test_module_level_singleton_exists(self):
        """wiki_cache_invalidator module-level singleton must be importable."""
        from code_indexer.server.wiki.wiki_cache_invalidator import wiki_cache_invalidator
        from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator

        assert isinstance(wiki_cache_invalidator, WikiCacheInvalidator)
