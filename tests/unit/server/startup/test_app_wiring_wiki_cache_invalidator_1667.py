"""
Tests for Bug #1667 fix: wire the orphaned WikiCacheInvalidator singleton
(since modular phase 4e by the cidx-wiki feature's on_startup, through the
core's repository_events).

Before this fix, `wiki_cache_invalidator.wiki_cache` was never set anywhere
in production. Three live call sites (mcp/handlers/git_write.py,
mcp/handlers/files.py x2) called its invalidate_* methods after every git
write / file mutation, but every one of those calls was a silent no-op
(`if self.wiki_cache is None: return`) because nothing ever wired a real
WikiCache instance into the singleton.

This matters because WikiCache.get_sidebar() has NO independent staleness
check (unlike get_article(), which self-validates via file_mtime/file_size)
-- Story #304 deliberately removed the old per-request filesystem mtime
poll in favor of this event-driven invalidator. Without wiring, the sidebar
cache could go stale indefinitely after any git write or file mutation.

Following the established structural-inspection pattern already used in
this codebase for app_wiring verification (see
test_app_wiring_consumer_rate_limiter_pool_1332.py) for the wiring-exists
assertions, and a genuine end-to-end round trip against a REAL SQLite-backed
WikiCache (no mocks) to prove the underlying invalidation mechanism itself
actually deletes cache rows once wired.
"""

import inspect
import sqlite3
from typing import Any, cast
from unittest.mock import patch

from code_indexer.server.features import repository_events
from code_indexer.server.features.repository_events import (
    CoreRepositoryEventsCapability,
)
from code_indexer.server.startup import app_wiring
from cidx_wiki.server.wiki_cache import WikiCache
from cidx_wiki.server.wiki_cache_invalidator import WikiCacheInvalidator


class TestFeatureStartupWiresWikiCacheInvalidator:
    """The wiki feature's on_startup subscribes the invalidator to the core's
    repository_events (modular phase 4e; formerly app_wiring set its cache):
    a file or git write the core announces drops the shared cache rows."""

    def _started(self, cache: WikiCache) -> Any:
        from cidx_wiki.server import lifecycle

        class _Context:
            def has_capability(self, name: str) -> bool:
                return name == "repository_events"

            def capability(self, name: str) -> object:
                return CoreRepositoryEventsCapability()

        started = lifecycle.WikiRuntime()
        with patch.object(lifecycle, "_shared_wiki_cache", return_value=cache):
            started.start(cast(Any, _Context()))
        return started

    def test_startup_subscribes_and_shutdown_unsubscribes(self, tmp_path) -> None:
        before = repository_events.subscriber_count()
        started = self._started(WikiCache(str(tmp_path / "w.db")))
        try:
            assert repository_events.subscriber_count() == before + 1
        finally:
            started.stop()
        assert repository_events.subscriber_count() == before

    def test_announced_markdown_change_drops_the_shared_rows(self, tmp_path) -> None:
        db_path = str(tmp_path / "w.db")
        cache = WikiCache(db_path)
        cache.ensure_tables()
        cache.put_sidebar("example-repo", [{"name": "x"}], tmp_path)
        started = self._started(cache)
        try:
            with patch(
                "cidx_wiki.server.lifecycle._shared_wiki_cache", return_value=cache
            ):
                repository_events.publish_content_changed("example-repo", "a.md")
        finally:
            started.stop()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM wiki_sidebar_cache WHERE repo_alias=?",
                ("example-repo",),
            ).fetchone()
        assert rows[0] == 0

    def test_app_wiring_no_longer_names_the_wiki(self) -> None:
        source = inspect.getsource(app_wiring)
        assert "wiki_cache_invalidator" not in source


class TestWikiCacheInvalidatorRealRoundTrip:
    """Real (no-mock) proof that, once wired, invalidation actually deletes rows.

    Uses a real SQLite-backed WikiCache (via DatabaseConnectionManager),
    a real WikiCacheInvalidator instance (not the process-wide singleton,
    to avoid cross-test pollution), and asserts real rows disappear.
    """

    def test_invalidate_repo_deletes_real_cached_article(self, tmp_path) -> None:
        db_path = str(tmp_path / "wiki_cache_1667.db")
        cache = WikiCache(db_path)
        cache.ensure_tables()

        article_file = tmp_path / "guide.md"
        article_file.write_text("# Guide\n")

        cache.put_article(
            repo_alias="my-repo",
            article_path="guide.md",
            html="<h1>Guide</h1>",
            title="Guide",
            file_path=article_file,
        )
        assert cache.get_article("my-repo", "guide.md", article_file) is not None

        invalidator = WikiCacheInvalidator()
        invalidator.set_wiki_cache(cache)
        invalidator.invalidate_repo("my-repo")

        assert cache.get_article("my-repo", "guide.md", article_file) is None

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM wiki_cache WHERE repo_alias = ?",
                ("my-repo",),
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()

    def test_invalidate_for_file_change_deletes_real_sidebar_cache(
        self, tmp_path
    ) -> None:
        db_path = str(tmp_path / "wiki_cache_sidebar_1667.db")
        cache = WikiCache(db_path)
        cache.ensure_tables()

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "guide.md").write_text("# Guide\n")

        cache.put_sidebar("my-repo", [{"path": "guide.md", "title": "Guide"}], repo_dir)
        assert cache.get_sidebar("my-repo", repo_dir) is not None

        invalidator = WikiCacheInvalidator()
        invalidator.set_wiki_cache(cache)
        # Simulates the real git_write.py / files.py call sites: a .md file
        # changed, so the sidebar (which has no independent staleness check)
        # must be invalidated.
        invalidator.invalidate_for_file_change("my-repo", "guide.md")

        assert cache.get_sidebar("my-repo", repo_dir) is None
