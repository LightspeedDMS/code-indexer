"""
Regression tests for Bug #1340 -- wiki view cleanup hook ERROR on golden-repo
removal: "no such table: wiki_article_views".

Root cause: ``wiki_article_views`` (Story #287) was never added to the
SQLite schema/migration path in ``DatabaseSchema.initialize_database()``
(``database_manager.py``).  It was only ever created lazily via
``WikiCache.ensure_tables()``, which is invoked exclusively from the
module-level singleton getter in ``server/wiki/routes.py`` on the FIRST
wiki HTTP route hit.  If a golden repo is removed before any wiki page has
ever been viewed on that node/process, the golden-repo-removal cleanup hook
(``GoldenRepoManager.remove_golden_repo``'s background worker) constructs a
fresh ``WikiCache`` and calls ``delete_views_for_repo()`` against a table
that does not yet exist, raising ``sqlite3.OperationalError: no such table:
wiki_article_views`` -- caught by the hook's broad except and logged at
ERROR (golden-repo removal itself still succeeds).

Fix: register a ``_migrate_wiki_article_views_table`` migration in
``DatabaseSchema.initialize_database()`` (same idempotent CREATE TABLE IF
NOT EXISTS idiom already used for ``wiki_cache``/``wiki_sidebar_cache``,
``server_config``, ``rate_limit_*``, ``token_blacklist``, etc.) so the
table exists at every server startup, closing the race entirely -- no
caller-side change needed.
"""

import logging
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "cidx_server.db")


class TestSchemaInitCreatesWikiArticleViewsTable:
    """DatabaseSchema.initialize_database() must create wiki_article_views."""

    def test_initialize_database_creates_wiki_article_views_table(self, temp_db_path):
        """A fresh schema init must create the wiki_article_views table."""
        DatabaseSchema(temp_db_path).initialize_database()

        conn = sqlite3.connect(temp_db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "wiki_article_views" in tables

    def test_wiki_article_views_table_has_correct_columns(self, temp_db_path):
        """wiki_article_views must have the schema used by WikiCache (Story #287)."""
        DatabaseSchema(temp_db_path).initialize_database()

        conn = sqlite3.connect(temp_db_path)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(wiki_article_views)").fetchall()
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

    def test_initialize_database_is_idempotent_for_wiki_article_views(
        self, temp_db_path
    ):
        """Running initialize_database() twice must not raise (CREATE TABLE IF NOT EXISTS)."""
        DatabaseSchema(temp_db_path).initialize_database()
        # Second run against an already-initialized DB must be a silent no-op.
        DatabaseSchema(temp_db_path).initialize_database()

        conn = sqlite3.connect(temp_db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "wiki_article_views" in tables

    def test_migration_applies_to_pre_existing_database_missing_the_table(
        self, temp_db_path
    ):
        """Bug #1340: an existing DB that predates Story #287 (has wiki_cache
        but not wiki_article_views) must get wiki_article_views added when
        initialize_database() runs again (schema drift repair on upgrade).
        """
        # Simulate an older DB: initialize schema, then drop the table to
        # emulate "this table was never created here" (pre-migration state).
        DatabaseSchema(temp_db_path).initialize_database()
        conn = sqlite3.connect(temp_db_path)
        conn.execute("DROP TABLE IF EXISTS wiki_article_views")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(temp_db_path)
        tables_before = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "wiki_article_views" not in tables_before

        # Re-running initialize_database() (as happens on every server restart)
        # must repair the missing table.
        DatabaseSchema(temp_db_path).initialize_database()

        conn = sqlite3.connect(temp_db_path)
        tables_after = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "wiki_article_views" in tables_after


class TestGoldenRepoRemovalWikiCleanupHookNoErrorBug1340:
    """End-to-end: golden-repo removal's wiki cleanup hook must not ERROR
    when the schema has been initialized via DatabaseSchema (the real
    server startup path), even if no wiki page was ever viewed.
    """

    def test_remove_golden_repo_does_not_log_wiki_cleanup_error(self, tmp_path, caplog):
        """Reproduces Bug #1340: before the fix, removing a golden repo on a
        freshly schema-initialized DB (no wiki route ever hit) raised
        'no such table: wiki_article_views' inside the wiki cleanup hook,
        logged at ERROR by golden_repo_manager.py's broad except.  After the
        fix (schema init creates the table), the hook must succeed silently.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
            GoldenRepo,
        )

        data_dir = str(tmp_path / "data")
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        manager = GoldenRepoManager(data_dir=data_dir)

        # This is the real production migration/init path (lifespan.py /
        # service_init.py both call this at every server startup) -- NOT a
        # manual ensure_tables() call and NOT a simulated wiki page view.
        DatabaseSchema(manager.db_path).initialize_database()

        # Register an existing golden repo (in-memory + SQLite backend +
        # real on-disk clone dir) mirroring a completed add_golden_repo(),
        # matching the pattern used in
        # test_golden_repo_manager_registry_orphan_bug1317.py.
        alias = "sched1337d"
        clone_path = Path(manager.golden_repos_dir) / alias
        clone_path.mkdir(parents=True, exist_ok=True)
        golden_repo = GoldenRepo(
            alias=alias,
            repo_url=f"https://github.com/test/{alias}.git",
            default_branch="main",
            clone_path=str(clone_path),
            created_at="2026-01-01T00:00:00Z",
            enable_temporal=False,
            temporal_options=None,
        )
        manager.golden_repos[alias] = golden_repo
        manager._sqlite_backend.add_repo(
            alias=golden_repo.alias,
            repo_url=golden_repo.repo_url,
            default_branch=golden_repo.default_branch,
            clone_path=golden_repo.clone_path,
            created_at=golden_repo.created_at,
            enable_temporal=golden_repo.enable_temporal,
            temporal_options=golden_repo.temporal_options,
        )

        # Mock background_job_manager to run the worker synchronously and
        # capture it, exactly like the Bug #1317 regression test does.
        mock_bg = MagicMock()
        manager.background_job_manager = mock_bg
        manager.activated_repo_manager = None
        manager.group_access_manager = None

        manager.remove_golden_repo(alias=alias, submitter_username="admin")
        background_worker = mock_bg.submit_job.call_args[1]["func"]

        with caplog.at_level(logging.ERROR):
            background_worker()

        wiki_errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR
            and "Wiki view cleanup hook failed" in r.getMessage()
        ]
        assert wiki_errors == [], (
            f"Expected no 'Wiki view cleanup hook failed' ERROR logs, got: "
            f"{[r.getMessage() for r in wiki_errors]}"
        )

        # The repo must actually be gone (removal itself still succeeds).
        assert alias not in manager.golden_repos

    def test_remove_golden_repo_wiki_cleanup_deletes_existing_view_records(
        self, tmp_path
    ):
        """Regression guard: the wiki cleanup hook's actual AC4 behavior
        (deleting pre-existing view records for the removed repo) must still
        work once the table is guaranteed to exist via schema init.
        """
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
            GoldenRepo,
        )
        from code_indexer.server.wiki.wiki_cache import WikiCache

        data_dir = str(tmp_path / "data")
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        manager = GoldenRepoManager(data_dir=data_dir)
        DatabaseSchema(manager.db_path).initialize_database()

        alias = "repo-with-views"
        clone_path = Path(manager.golden_repos_dir) / alias
        clone_path.mkdir(parents=True, exist_ok=True)
        golden_repo = GoldenRepo(
            alias=alias,
            repo_url=f"https://github.com/test/{alias}.git",
            default_branch="main",
            clone_path=str(clone_path),
            created_at="2026-01-01T00:00:00Z",
            enable_temporal=False,
            temporal_options=None,
        )
        manager.golden_repos[alias] = golden_repo
        manager._sqlite_backend.add_repo(
            alias=golden_repo.alias,
            repo_url=golden_repo.repo_url,
            default_branch=golden_repo.default_branch,
            clone_path=golden_repo.clone_path,
            created_at=golden_repo.created_at,
            enable_temporal=golden_repo.enable_temporal,
            temporal_options=golden_repo.temporal_options,
        )

        # Pre-existing view record for the repo being removed, written
        # directly against the same db_path the removal hook will use.
        wiki_cache = WikiCache(manager.db_path)
        wiki_cache.increment_view(alias, "docs/intro")
        assert wiki_cache.get_view_count(alias, "docs/intro") == 1

        mock_bg = MagicMock()
        manager.background_job_manager = mock_bg
        manager.activated_repo_manager = None
        manager.group_access_manager = None

        manager.remove_golden_repo(alias=alias, submitter_username="admin")
        background_worker = mock_bg.submit_job.call_args[1]["func"]
        background_worker()

        assert wiki_cache.get_all_view_counts(alias) == []
