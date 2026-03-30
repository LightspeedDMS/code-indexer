"""
Unit tests for ActivatedRepoManager cluster mode (Bug #587).

Tests dual-mode metadata operations: PostgreSQL for cluster mode,
JSON files for standalone mode. Follows the same dual-mode pattern
used by LoginRateLimiter, TOTPService, MfaChallengeManager.
"""

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


class _SqliteCursor:
    """Adapts sqlite3.Cursor to behave like psycopg cursor with %s placeholders."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._cursor = conn.cursor()

    def execute(self, sql: str, params=None):
        """Execute SQL, converting %s placeholders to ? for SQLite."""
        sql = sql.replace("%s", "?")
        if params:
            self._cursor.execute(sql, params)
        else:
            self._cursor.execute(sql)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cursor.fetchall()
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, row)) for row in rows]


class _SqliteConnection:
    """Wraps sqlite3.Connection to mimic psycopg connection with dict rows."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.row_factory = None

    def execute(self, sql: str, params=None):
        return _SqliteCursor(self._conn).execute(sql, params)

    def commit(self):
        self._conn.commit()


class SqlitePoolAdapter:
    """Simulates psycopg ConnectionPool using SQLite for unit tests.

    This is a test adapter (not a mock) that uses a real SQLite database
    to verify PG-mode logic without requiring a PostgreSQL instance.
    Justified: PostgreSQL is an external service.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._create_schema()

    def _create_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS activated_repos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                user_alias TEXT NOT NULL,
                golden_repo_alias TEXT,
                repo_path TEXT NOT NULL,
                current_branch TEXT DEFAULT 'main',
                activated_at TEXT,
                last_accessed TEXT,
                git_committer_email TEXT,
                ssh_key_used INTEGER DEFAULT 0,
                is_composite INTEGER DEFAULT 0,
                wiki_enabled INTEGER DEFAULT 0,
                metadata_json TEXT,
                UNIQUE(username, user_alias)
            )
        """)
        self._conn.commit()

    @contextmanager
    def connection(self):
        yield _SqliteConnection(self._conn)

    def close(self):
        self._conn.close()


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def manager(temp_data_dir):
    """Create ActivatedRepoManager with temp data dir and mocked dependencies."""
    golden_mock = MagicMock()
    bg_mock = MagicMock()
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_mock,
        background_job_manager=bg_mock,
    )


class TestSetConnectionPool:
    """Tests for set_connection_pool method."""

    def test_set_connection_pool_enables_pg_mode(self, manager):
        """set_connection_pool should store the pool reference."""
        mock_pool = MagicMock()
        manager.set_connection_pool(mock_pool)
        assert manager._pool is mock_pool

    def test_pool_is_none_by_default(self, manager):
        """_pool should be None by default (standalone mode)."""
        assert manager._pool is None


class TestSetSharedReposDir:
    """Tests for set_shared_repos_dir method."""

    def test_set_shared_repos_dir_changes_path(self, manager, temp_data_dir):
        """set_shared_repos_dir should update activated_repos_dir to shared path."""
        shared_dir = os.path.join(temp_data_dir, "nfs-shared")
        os.makedirs(shared_dir)
        manager.set_shared_repos_dir(shared_dir)
        expected = os.path.join(shared_dir, "activated-repos")
        assert manager.activated_repos_dir == expected
        assert os.path.isdir(expected)


class TestMetadataFileOperations:
    """Tests for dual-mode metadata save/load in standalone (file) mode."""

    def test_save_and_load_metadata_file_standalone(self, manager, temp_data_dir):
        """_save_metadata should write JSON file, _load_metadata should read it back."""
        metadata = {
            "user_alias": "my-repo",
            "golden_repo_alias": "golden-test",
            "current_branch": "main",
            "activated_at": "2026-03-29T00:00:00+00:00",
            "last_accessed": "2026-03-29T00:00:00+00:00",
            "path": "/some/path",
        }
        manager._save_metadata("testuser", "my-repo", metadata)

        loaded = manager._load_metadata("testuser", "my-repo")
        assert loaded is not None
        assert loaded["user_alias"] == "my-repo"
        assert loaded["golden_repo_alias"] == "golden-test"
        assert loaded["current_branch"] == "main"

    def test_delete_metadata_file_standalone(self, manager):
        """_delete_metadata removes JSON file; _load_metadata returns None."""
        metadata = {
            "user_alias": "del-repo",
            "golden_repo_alias": "golden-del",
            "current_branch": "main",
            "activated_at": "2026-03-29T00:00:00+00:00",
            "last_accessed": "2026-03-29T00:00:00+00:00",
            "path": "/some/path",
        }
        manager._save_metadata("testuser", "del-repo", metadata)
        assert manager._load_metadata("testuser", "del-repo") is not None

        manager._delete_metadata("testuser", "del-repo")
        assert manager._load_metadata("testuser", "del-repo") is None

    def test_list_user_repos_file_standalone(self, manager):
        """_list_user_repos should return all repos for a user from JSON files."""
        for alias in ("repo-a", "repo-b"):
            metadata = {
                "user_alias": alias,
                "golden_repo_alias": f"golden-{alias}",
                "current_branch": "main",
                "activated_at": "2026-03-29T00:00:00+00:00",
                "last_accessed": "2026-03-29T00:00:00+00:00",
                "path": f"/some/{alias}",
            }
            manager._save_metadata("listuser", alias, metadata)
            # Create matching repo directory (required by list logic)
            repo_dir = os.path.join(manager.activated_repos_dir, "listuser", alias)
            os.makedirs(repo_dir, exist_ok=True)

        repos = manager._list_user_repos("listuser")
        aliases = {r["user_alias"] for r in repos}
        assert aliases == {"repo-a", "repo-b"}

    def test_list_all_repos_file_standalone(self, manager):
        """_list_all_repos should return repos across all users."""
        for username, alias in [("user1", "r1"), ("user2", "r2")]:
            metadata = {
                "user_alias": alias,
                "golden_repo_alias": f"golden-{alias}",
                "current_branch": "main",
                "activated_at": "2026-03-29T00:00:00+00:00",
                "last_accessed": "2026-03-29T00:00:00+00:00",
                "path": f"/some/{alias}",
            }
            manager._save_metadata(username, alias, metadata)
            repo_dir = os.path.join(manager.activated_repos_dir, username, alias)
            os.makedirs(repo_dir, exist_ok=True)

        all_repos = manager._list_all_repos()
        aliases = {r["user_alias"] for r in all_repos}
        assert aliases == {"r1", "r2"}


@pytest.fixture
def pg_pool(temp_data_dir):
    """Create SQLite-backed pool adapter simulating psycopg for PG tests."""
    db_path = os.path.join(temp_data_dir, "test_cluster.db")
    pool = SqlitePoolAdapter(db_path)
    yield pool
    pool.close()


@pytest.fixture
def pg_manager(temp_data_dir, pg_pool):
    """Create ActivatedRepoManager wired to PG pool adapter."""
    golden_mock = MagicMock()
    bg_mock = MagicMock()
    mgr = ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_mock,
        background_job_manager=bg_mock,
    )
    mgr.set_connection_pool(pg_pool)
    return mgr


class TestMetadataPgOperations:
    """Tests for PG-backed metadata operations in cluster mode."""

    def test_save_metadata_pg_writes_to_table(self, pg_manager, pg_pool):
        """_save_metadata should INSERT into activated_repos when pool is set."""
        metadata = {
            "user_alias": "pg-repo",
            "golden_repo_alias": "golden-pg",
            "current_branch": "develop",
            "activated_at": "2026-03-29T00:00:00+00:00",
            "last_accessed": "2026-03-29T00:00:00+00:00",
            "path": "/nfs/activated-repos/user1/pg-repo",
        }
        pg_manager._save_metadata("pguser", "pg-repo", metadata)

        # Verify directly in DB
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM activated_repos WHERE username = %s AND user_alias = %s",
                ("pguser", "pg-repo"),
            ).fetchone()
        assert row is not None
        assert row["golden_repo_alias"] == "golden-pg"
        assert row["repo_path"] == "/nfs/activated-repos/user1/pg-repo"
        assert row["current_branch"] == "develop"

    def test_load_metadata_pg_reads_from_table(self, pg_manager):
        """_load_metadata should read from activated_repos table when pool is set."""
        metadata = {
            "user_alias": "load-repo",
            "golden_repo_alias": "golden-load",
            "current_branch": "feature-x",
            "activated_at": "2026-03-29T01:00:00+00:00",
            "last_accessed": "2026-03-29T02:00:00+00:00",
            "path": "/nfs/load-repo",
            "git_committer_email": "test@example.com",
            "ssh_key_used": True,
            "is_composite": False,
            "wiki_enabled": True,
        }
        pg_manager._save_metadata("loaduser", "load-repo", metadata)

        loaded = pg_manager._load_metadata("loaduser", "load-repo")
        assert loaded is not None
        assert loaded["user_alias"] == "load-repo"
        assert loaded["golden_repo_alias"] == "golden-load"
        assert loaded["current_branch"] == "feature-x"
        assert loaded["path"] == "/nfs/load-repo"
        assert loaded["git_committer_email"] == "test@example.com"
        assert loaded["ssh_key_used"] is True
        assert loaded["wiki_enabled"] is True

    def test_delete_metadata_pg_removes_row(self, pg_manager):
        """_delete_metadata should DELETE from activated_repos table."""
        metadata = {
            "user_alias": "del-pg",
            "golden_repo_alias": "golden-del",
            "current_branch": "main",
            "activated_at": "2026-03-29T00:00:00+00:00",
            "last_accessed": "2026-03-29T00:00:00+00:00",
            "path": "/nfs/del-pg",
        }
        pg_manager._save_metadata("deluser", "del-pg", metadata)
        assert pg_manager._load_metadata("deluser", "del-pg") is not None

        pg_manager._delete_metadata("deluser", "del-pg")
        assert pg_manager._load_metadata("deluser", "del-pg") is None

    def test_list_user_repos_pg_returns_user_repos(self, pg_manager):
        """_list_user_repos should return repos for a specific user from PG."""
        for alias in ("pg-a", "pg-b"):
            metadata = {
                "user_alias": alias,
                "golden_repo_alias": f"golden-{alias}",
                "current_branch": "main",
                "activated_at": "2026-03-29T00:00:00+00:00",
                "last_accessed": "2026-03-29T00:00:00+00:00",
                "path": f"/nfs/{alias}",
            }
            pg_manager._save_metadata("pglistuser", alias, metadata)

        repos = pg_manager._list_user_repos("pglistuser")
        aliases = {r["user_alias"] for r in repos}
        assert aliases == {"pg-a", "pg-b"}

    def test_list_all_repos_pg_returns_all_repos(self, pg_manager):
        """_list_all_repos should return repos across all users from PG."""
        for username, alias in [("u1", "r1"), ("u2", "r2")]:
            metadata = {
                "user_alias": alias,
                "golden_repo_alias": f"golden-{alias}",
                "current_branch": "main",
                "activated_at": "2026-03-29T00:00:00+00:00",
                "last_accessed": "2026-03-29T00:00:00+00:00",
                "path": f"/nfs/{alias}",
            }
            pg_manager._save_metadata(username, alias, metadata)

        all_repos = pg_manager._list_all_repos()
        aliases = {r["user_alias"] for r in all_repos}
        assert "r1" in aliases
        assert "r2" in aliases
