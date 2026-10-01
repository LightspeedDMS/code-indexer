"""
Tests for Bug #1667 fix: wire the orphaned WikiCacheInvalidator singleton.

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

from code_indexer.server.startup import app_wiring
from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.wiki.wiki_cache_invalidator import WikiCacheInvalidator


class TestAppWiringWiresWikiCacheInvalidator:
    """Structural verification that create_fastapi_app wires the invalidator."""

    def test_app_wiring_source_references_wiki_cache_invalidator(self) -> None:
        source = inspect.getsource(app_wiring)
        assert "wiki_cache_invalidator" in source

    def test_app_wiring_calls_set_wiki_cache(self) -> None:
        source = inspect.getsource(app_wiring.create_fastapi_app)
        assert "wiki_cache_invalidator.set_wiki_cache(" in source

    def test_wiring_falls_back_to_none_backend_when_registry_absent(self) -> None:
        """Solo/SQLite mode may have backend_registry=None -- the wiring must
        guard against that and pass storage_backend=None (WikiCache's own
        SQLite fallback), never crash on a None.wiki_cache attribute access."""
        source = inspect.getsource(app_wiring.create_fastapi_app)
        assert (
            "backend_registry.wiki_cache if backend_registry is not None else None"
            in source
        )


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
