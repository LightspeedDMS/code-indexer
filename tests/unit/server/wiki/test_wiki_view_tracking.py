"""Tests for wiki article view tracking (Story #287).

RED phase: all tests written before implementation exists.
"""
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.wiki.wiki_service import WikiService
from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid, get_current_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_db():
    """Provide a temp SQLite db path with tables created for each test."""
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


def _make_user(username):
    user = MagicMock()
    user.username = username
    return user


def _make_app(actual_repo_path, db_path, authenticated_user=None,
              user_accessible_repos=None, wiki_enabled=True):
    """Build a FastAPI test app with wiki router and minimal app state."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    if authenticated_user:
        app.dependency_overrides[get_wiki_user_hybrid] = lambda: authenticated_user
        app.dependency_overrides[get_current_user_hybrid] = lambda: authenticated_user

    app.include_router(wiki_router, prefix="/wiki")

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = wiki_enabled
    app.state.golden_repo_manager.db_path = db_path

    golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
    app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    app.state.access_filtering_service.get_accessible_repos.return_value = (
        user_accessible_repos or {"test-repo"}
    )
    return app


# ---------------------------------------------------------------------------
# AC1: wiki_article_views table creation
# ---------------------------------------------------------------------------


class TestEnsureTablesCreatesViewTable:
    def test_ensure_tables_creates_wiki_article_views_table(self, cache_db):
        """ensure_tables() must create the wiki_article_views table."""
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "wiki_article_views" in tables

    def test_wiki_article_views_table_has_correct_columns(self, cache_db):
        """wiki_article_views must have the exact schema specified in AC1."""
        cache, db_path = cache_db
        conn = sqlite3.connect(db_path)
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(wiki_article_views)"
            ).fetchall()
        }
        conn.close()
        expected = {
            "repo_alias",
            "article_path",
            "real_views",
            "first_viewed_at",
            "last_viewed_at",
        }
        assert expected.issubset(cols)


# ---------------------------------------------------------------------------
# AC1: increment_view
# ---------------------------------------------------------------------------


class TestIncrementView:
    def test_increment_view_creates_new_row_on_first_view(self, cache_db):
        """First increment_view call must insert a row with real_views=1."""
        cache, _ = cache_db
        cache.increment_view("repo1", "docs/intro")
        count = cache.get_view_count("repo1", "docs/intro")
        assert count == 1

    def test_increment_view_increments_existing_row(self, cache_db):
        """Second increment_view call must raise real_views from 1 to 2."""
        cache, _ = cache_db
        cache.increment_view("repo1", "docs/intro")
        cache.increment_view("repo1", "docs/intro")
        count = cache.get_view_count("repo1", "docs/intro")
        assert count == 2

    def test_increment_view_sets_first_viewed_at_on_insert(self, cache_db):
        """first_viewed_at must be non-NULL after initial insert."""
        cache, db_path = cache_db
        cache.increment_view("repo1", "docs/intro")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT first_viewed_at FROM wiki_article_views "
            "WHERE repo_alias=? AND article_path=?",
            ("repo1", "docs/intro"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] is not None

    def test_increment_view_preserves_first_viewed_at_on_update(self, cache_db):
        """first_viewed_at must not change when view count is incremented."""
        cache, db_path = cache_db
        cache.increment_view("repo1", "docs/intro")
        conn = sqlite3.connect(db_path)
        first_ts = conn.execute(
            "SELECT first_viewed_at FROM wiki_article_views "
            "WHERE repo_alias=? AND article_path=?",
            ("repo1", "docs/intro"),
        ).fetchone()[0]
        conn.close()

        cache.increment_view("repo1", "docs/intro")
        conn = sqlite3.connect(db_path)
        after_ts = conn.execute(
            "SELECT first_viewed_at FROM wiki_article_views "
            "WHERE repo_alias=? AND article_path=?",
            ("repo1", "docs/intro"),
        ).fetchone()[0]
        conn.close()
        assert first_ts == after_ts

    def test_increment_view_updates_last_viewed_at_on_update(self, cache_db):
        """last_viewed_at must be updated on every increment."""
        cache, db_path = cache_db
        cache.increment_view("repo1", "docs/intro")
        conn = sqlite3.connect(db_path)
        ts1 = conn.execute(
            "SELECT last_viewed_at FROM wiki_article_views "
            "WHERE repo_alias=? AND article_path=?",
            ("repo1", "docs/intro"),
        ).fetchone()[0]
        conn.close()

        time.sleep(0.01)
        cache.increment_view("repo1", "docs/intro")
        conn = sqlite3.connect(db_path)
        ts2 = conn.execute(
            "SELECT last_viewed_at FROM wiki_article_views "
            "WHERE repo_alias=? AND article_path=?",
            ("repo1", "docs/intro"),
        ).fetchone()[0]
        conn.close()
        assert ts2 >= ts1

    def test_increment_view_isolates_per_repo(self, cache_db):
        """increment_view on repo1 must not affect repo2 counts."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page")
        cache.increment_view("repo1", "page")
        cache.increment_view("repo2", "page")
        assert cache.get_view_count("repo1", "page") == 2
        assert cache.get_view_count("repo2", "page") == 1


# ---------------------------------------------------------------------------
# AC1: get_view_count
# ---------------------------------------------------------------------------


class TestGetViewCount:
    def test_get_view_count_returns_correct_count(self, cache_db):
        """get_view_count must return the current real_views value."""
        cache, _ = cache_db
        cache.increment_view("repo1", "article")
        cache.increment_view("repo1", "article")
        cache.increment_view("repo1", "article")
        assert cache.get_view_count("repo1", "article") == 3

    def test_get_view_count_returns_zero_for_nonexistent(self, cache_db):
        """get_view_count must return 0 for an article that has never been viewed."""
        cache, _ = cache_db
        assert cache.get_view_count("repo1", "never-seen") == 0


# ---------------------------------------------------------------------------
# AC1: get_all_view_counts
# ---------------------------------------------------------------------------


class TestGetAllViewCounts:
    def test_get_all_view_counts_returns_all_records_for_repo(self, cache_db):
        """get_all_view_counts must return all view records for one repo."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo1", "page-b")
        cache.increment_view("repo2", "page-x")

        results = cache.get_all_view_counts("repo1")
        assert len(results) == 2
        paths = {r["article_path"] for r in results}
        assert "page-a" in paths
        assert "page-b" in paths

    def test_get_all_view_counts_includes_correct_counts(self, cache_db):
        """get_all_view_counts must include correct real_views for each article."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo1", "page-b")

        results = {r["article_path"]: r["real_views"] for r in cache.get_all_view_counts("repo1")}
        assert results["page-a"] == 2
        assert results["page-b"] == 1

    def test_get_all_view_counts_excludes_other_repos(self, cache_db):
        """get_all_view_counts must not include records from other repos."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo2", "page-x")

        results = cache.get_all_view_counts("repo1")
        paths = {r["article_path"] for r in results}
        assert "page-x" not in paths

    def test_get_all_view_counts_empty_for_unknown_repo(self, cache_db):
        """get_all_view_counts must return empty list for unknown repo."""
        cache, _ = cache_db
        assert cache.get_all_view_counts("unknown-repo") == []


# ---------------------------------------------------------------------------
# AC4: delete_views_for_repo
# ---------------------------------------------------------------------------


class TestDeleteViewsForRepo:
    def test_delete_views_for_repo_removes_all_records(self, cache_db):
        """delete_views_for_repo must delete all records for the target repo."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo1", "page-b")
        cache.delete_views_for_repo("repo1")
        assert cache.get_all_view_counts("repo1") == []

    def test_delete_views_for_repo_does_not_affect_other_repos(self, cache_db):
        """delete_views_for_repo must not delete records from other repos."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo2", "page-b")
        cache.delete_views_for_repo("repo1")
        results = cache.get_all_view_counts("repo2")
        assert len(results) == 1
        assert results[0]["article_path"] == "page-b"


# ---------------------------------------------------------------------------
# AC2: populate_views_from_front_matter
# ---------------------------------------------------------------------------


class TestPopulateViewsFromFrontMatter:
    def test_populate_creates_rows_from_front_matter_views_field(self, cache_db, repo_dir):
        """populate_views_from_front_matter must insert rows for articles with 'views' in front matter."""
        cache, _ = cache_db
        md_content = "---\ntitle: Intro\nviews: 42\n---\n# Intro\nContent here."
        (repo_dir / "intro.md").write_text(md_content)

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        count = cache.get_view_count("repo1", "intro")
        assert count == 42

    def test_populate_skips_files_without_views_field(self, cache_db, repo_dir):
        """populate_views_from_front_matter must skip files whose front matter has no 'views' field."""
        cache, _ = cache_db
        (repo_dir / "no-views.md").write_text("---\ntitle: No Views\n---\n# No Views")

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        assert cache.get_view_count("repo1", "no-views") == 0

    def test_populate_skips_files_without_front_matter(self, cache_db, repo_dir):
        """populate_views_from_front_matter must skip plain markdown files with no front matter."""
        cache, _ = cache_db
        (repo_dir / "plain.md").write_text("# Plain Article\nNo front matter.")

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        assert cache.get_view_count("repo1", "plain") == 0

    def test_populate_handles_nested_paths(self, cache_db, repo_dir):
        """populate_views_from_front_matter must store article_path relative to repo root without .md extension."""
        cache, _ = cache_db
        subdir = repo_dir / "guides"
        subdir.mkdir()
        (subdir / "getting-started.md").write_text(
            "---\ntitle: Getting Started\nviews: 100\n---\n# Getting Started"
        )

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        count = cache.get_view_count("repo1", "guides/getting-started")
        assert count == 100

    def test_populate_skips_if_records_already_exist(self, cache_db, repo_dir):
        """populate_views_from_front_matter must skip entirely if records already exist for this repo (AC5)."""
        cache, _ = cache_db
        # Pre-existing views record for this repo
        cache.increment_view("repo1", "existing-page")
        assert cache.get_view_count("repo1", "existing-page") == 1

        # Write a markdown file with 99 views in front matter
        (repo_dir / "new-article.md").write_text(
            "---\ntitle: New Article\nviews: 99\n---\n# New Article"
        )

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        # Must NOT have inserted the 99-view record
        assert cache.get_view_count("repo1", "new-article") == 0
        # Must NOT have touched the existing record
        assert cache.get_view_count("repo1", "existing-page") == 1

    def test_populate_skips_hidden_directories(self, cache_db, repo_dir):
        """populate_views_from_front_matter must not process files in hidden directories."""
        cache, _ = cache_db
        hidden = repo_dir / ".git"
        hidden.mkdir()
        (hidden / "internal.md").write_text("---\nviews: 5\n---\n# Internal")

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        assert cache.get_view_count("repo1", ".git/internal") == 0


# ---------------------------------------------------------------------------
# Route integration: view increment on article load
# ---------------------------------------------------------------------------


class TestRouteIncrementsView:
    def test_wiki_article_load_increments_view_count(self):
        """Serving a wiki article must call increment_view so view count increases."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                (repo_dir / "article.md").write_text("# Article\nContent here.")
                app = _make_app(
                    actual_repo_path=tmpdir,
                    db_path=db_path,
                    authenticated_user=_make_user("alice"),
                )

                cache = WikiCache(db_path)
                cache.ensure_tables()

                client = TestClient(app)
                resp = client.get("/wiki/test-repo/article")
                assert resp.status_code == 200

                count = cache.get_view_count("test-repo", "article")
                assert count == 1
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_wiki_root_load_increments_view_count(self):
        """Serving the wiki root (home.md) must also increment view count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                (repo_dir / "home.md").write_text("# Home\nWelcome.")
                app = _make_app(
                    actual_repo_path=tmpdir,
                    db_path=db_path,
                    authenticated_user=_make_user("alice"),
                )

                cache = WikiCache(db_path)
                cache.ensure_tables()

                client = TestClient(app)
                resp = client.get("/wiki/test-repo/")
                assert resp.status_code == 200

                # home.md is tracked as empty string path (root article)
                count = cache.get_view_count("test-repo", "")
                assert count == 1
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_wiki_article_view_count_accumulates(self):
        """Loading the same article multiple times must accumulate the view count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                repo_dir = Path(tmpdir)
                (repo_dir / "popular.md").write_text("# Popular\nContent.")
                app = _make_app(
                    actual_repo_path=tmpdir,
                    db_path=db_path,
                    authenticated_user=_make_user("alice"),
                )

                cache = WikiCache(db_path)
                cache.ensure_tables()

                client = TestClient(app)
                client.get("/wiki/test-repo/popular")
                client.get("/wiki/test-repo/popular")
                client.get("/wiki/test-repo/popular")

                count = cache.get_view_count("test-repo", "popular")
                assert count == 3
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# AC3: Wiki disable does NOT delete view records
# ---------------------------------------------------------------------------


class TestWikiDisablePreservesViewRecords:
    def test_view_records_are_preserved_after_wiki_disable(self, cache_db):
        """Disabling wiki must not delete wiki_article_views records.

        The web route toggle_wiki_enabled() only calls manager.set_wiki_enabled()
        and does NOT touch wiki_article_views, so records are preserved.
        """
        cache, _ = cache_db
        cache.increment_view("repo1", "my-article")
        assert cache.get_view_count("repo1", "my-article") == 1

        # Simulate what wiki disable does: only invalidates render cache
        cache.invalidate_repo("repo1")

        # View records must still be present
        assert cache.get_view_count("repo1", "my-article") == 1

    def test_invalidate_repo_does_not_touch_view_records(self, cache_db):
        """invalidate_repo (render cache clear) must leave wiki_article_views untouched."""
        cache, _ = cache_db
        cache.increment_view("repo1", "page-a")
        cache.increment_view("repo1", "page-b")

        cache.invalidate_repo("repo1")

        results = cache.get_all_view_counts("repo1")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Fix 4: Concurrency — increment_view must be atomic under concurrent load
# ---------------------------------------------------------------------------


class TestIncrementViewConcurrency:
    def test_concurrent_increments_are_atomic(self, cache_db):
        """10 threads each increment 100 times. Final count must be 1000."""
        import threading

        cache, _ = cache_db

        def increment_n_times():
            for _ in range(100):
                cache.increment_view("repo1", "article1")

        threads = [threading.Thread(target=increment_n_times) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cache.get_view_count("repo1", "article1") == 1000


# ---------------------------------------------------------------------------
# Fix 5: Edge cases — non-numeric and negative views fields in front matter
# ---------------------------------------------------------------------------


class TestPopulateViewsFrontMatterEdgeCases:
    def test_populate_skips_non_numeric_views_field(self, cache_db, repo_dir):
        """populate_views_from_front_matter must skip files with non-numeric views field."""
        cache, _ = cache_db
        (repo_dir / "bad-views.md").write_text(
            "---\ntitle: Bad Views\nviews: not-a-number\n---\n# Bad Views"
        )

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        assert cache.get_view_count("repo1", "bad-views") == 0

    def test_populate_skips_negative_views_field(self, cache_db, repo_dir):
        """populate_views_from_front_matter must skip files with negative views field."""
        cache, _ = cache_db
        (repo_dir / "negative-views.md").write_text(
            "---\ntitle: Negative Views\nviews: -5\n---\n# Negative Views"
        )

        service = WikiService()
        service.populate_views_from_front_matter("repo1", repo_dir, cache)

        assert cache.get_view_count("repo1", "negative-views") == 0
