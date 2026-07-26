"""activation_id generation + persistence (Story #1458 AC11, Finding 7).

`ActivatedRepoManager` records only an `activated_at` ISO-8601 timestamp at
second resolution today, which is collision-prone within one clock tick (a
deactivate+reactivate inside the same second would reuse the same token) and
is therefore INSUFFICIENT as a per-clone generation/identity token on its
own. This adds a dedicated, guaranteed-unique per-activation UUID
`activation_id`, generated exactly once per activation (at the SAME two code
sites that already assign `activated_at`) and persisted in BOTH the
JSON-file and PostgreSQL metadata backends, alongside `activated_at` --
never as a replacement for it.

Real JSON-file round-trips (real files), and a REAL SQLite-backed adapter
for the PostgreSQL code path (`SqlitePoolAdapter`, the SAME established,
project-sanctioned "test adapter, not a mock" pattern already used by
tests/unit/server/repositories/test_activated_repos_cluster.py -- PostgreSQL
itself is an external service, justifying this substitution). The full
`_do_activate_repository` worker is invoked for real (extracted from the
mocked BackgroundJobManager's captured call, mirroring this file's own
`test_activated_repo_manager.py` fixture conventions) to prove the
generation site is genuinely wired into production activation, not a
disconnected reader.
"""

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from src.code_indexer.server.utils.config_manager import ServerResourceConfig

# Captured at module import time, BEFORE any per-test @patch("os.path.exists")
# replaces the attribute -- a reference captured INSIDE a patched test would
# actually be the mock itself, causing infinite recursion when delegated to.
_REAL_OS_PATH_EXISTS = os.path.exists


class _SqliteCursor:
    def __init__(self, conn: sqlite3.Connection):
        self._cursor = conn.cursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._cursor.close()
        return False

    def execute(self, sql: str, params=None):
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
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params=None):
        return _SqliteCursor(self._conn).execute(sql, params)

    def cursor(self, row_factory=None):
        return _SqliteCursor(self._conn)

    def commit(self):
        self._conn.commit()


class SqlitePoolAdapter:
    """Real SQLite-backed test adapter simulating psycopg's ConnectionPool
    (same established pattern as test_activated_repos_cluster.py) --
    schema mirrors the REAL migration 011 + the new activation_id column
    this story adds via a backward-compatible ADD COLUMN migration."""

    def __init__(self, db_path: str):
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
                activation_id TEXT,
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
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


class TestActivationIdFileModeRoundTrip:
    def test_activation_id_saved_and_loaded_via_json_file(self, temp_data_dir):
        manager = ActivatedRepoManager(data_dir=temp_data_dir)
        metadata = {
            "user_alias": "my-repo",
            "golden_repo_alias": "golden-test",
            "current_branch": "main",
            "activated_at": "2026-07-24T00:00:00+00:00",
            "last_accessed": "2026-07-24T00:00:00+00:00",
            "path": "/some/path",
            "activation_id": "11111111-1111-1111-1111-111111111111",
        }
        manager._save_metadata("testuser", "my-repo", metadata)

        loaded = manager._load_metadata("testuser", "my-repo")

        assert loaded is not None
        assert loaded["activation_id"] == "11111111-1111-1111-1111-111111111111"

    def test_get_activation_id_reads_persisted_value(self, temp_data_dir):
        manager = ActivatedRepoManager(data_dir=temp_data_dir)
        metadata = {
            "user_alias": "my-repo",
            "golden_repo_alias": "golden-test",
            "current_branch": "main",
            "activated_at": "2026-07-24T00:00:00+00:00",
            "last_accessed": "2026-07-24T00:00:00+00:00",
            "path": "/some/path",
            "activation_id": "22222222-2222-2222-2222-222222222222",
        }
        manager._save_metadata("testuser", "my-repo", metadata)

        assert (
            manager.get_activation_id("testuser", "my-repo")
            == "22222222-2222-2222-2222-222222222222"
        )

    def test_get_activation_id_returns_none_for_legacy_metadata_without_field(
        self, temp_data_dir
    ):
        # Backward compatibility: metadata written before this story's field
        # existed simply has no "activation_id" key at all.
        manager = ActivatedRepoManager(data_dir=temp_data_dir)
        metadata = {
            "user_alias": "legacy-repo",
            "golden_repo_alias": "golden-legacy",
            "current_branch": "main",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "last_accessed": "2026-01-01T00:00:00+00:00",
            "path": "/some/legacy/path",
        }
        manager._save_metadata("testuser", "legacy-repo", metadata)

        assert manager.get_activation_id("testuser", "legacy-repo") is None

    def test_get_activation_id_returns_none_when_repo_not_activated(
        self, temp_data_dir
    ):
        manager = ActivatedRepoManager(data_dir=temp_data_dir)
        assert manager.get_activation_id("nouser", "norepo") is None


@pytest.fixture
def pg_pool(temp_data_dir):
    db_path = os.path.join(temp_data_dir, "test_cluster.db")
    pool = SqlitePoolAdapter(db_path)
    yield pool
    pool.close()


@pytest.fixture
def pg_manager(temp_data_dir, pg_pool):
    mgr = ActivatedRepoManager(data_dir=temp_data_dir)
    mgr.set_connection_pool(pg_pool)
    return mgr


class TestActivationIdPgModeRoundTrip:
    def test_activation_id_written_to_dedicated_pg_column(self, pg_manager, pg_pool):
        metadata = {
            "user_alias": "pg-repo",
            "golden_repo_alias": "golden-pg",
            "current_branch": "main",
            "activated_at": "2026-07-24T00:00:00+00:00",
            "last_accessed": "2026-07-24T00:00:00+00:00",
            "path": "/nfs/activated-repos/user1/pg-repo",
            "activation_id": "33333333-3333-3333-3333-333333333333",
        }
        pg_manager._save_metadata("pguser", "pg-repo", metadata)

        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM activated_repos WHERE username = %s AND user_alias = %s",
                ("pguser", "pg-repo"),
            ).fetchone()

        # Written to the DEDICATED column, not stuffed into metadata_json.
        assert row["activation_id"] == "33333333-3333-3333-3333-333333333333"
        metadata_json = row["metadata_json"]
        assert metadata_json is None or "activation_id" not in metadata_json

    def test_activation_id_round_trips_via_load_metadata(self, pg_manager):
        metadata = {
            "user_alias": "load-repo",
            "golden_repo_alias": "golden-load",
            "current_branch": "main",
            "activated_at": "2026-07-24T01:00:00+00:00",
            "last_accessed": "2026-07-24T02:00:00+00:00",
            "path": "/nfs/load-repo",
            "activation_id": "44444444-4444-4444-4444-444444444444",
        }
        pg_manager._save_metadata("loaduser", "load-repo", metadata)

        loaded = pg_manager._load_metadata("loaduser", "load-repo")

        assert loaded["activation_id"] == "44444444-4444-4444-4444-444444444444"

    def test_get_activation_id_via_pg_backend(self, pg_manager):
        metadata = {
            "user_alias": "get-repo",
            "golden_repo_alias": "golden-get",
            "current_branch": "main",
            "activated_at": "2026-07-24T01:00:00+00:00",
            "last_accessed": "2026-07-24T02:00:00+00:00",
            "path": "/nfs/get-repo",
            "activation_id": "55555555-5555-5555-5555-555555555555",
        }
        pg_manager._save_metadata("getuser", "get-repo", metadata)

        assert (
            pg_manager.get_activation_id("getuser", "get-repo")
            == "55555555-5555-5555-5555-555555555555"
        )

    def test_get_activation_id_returns_none_for_pre_migration_null_column(
        self, pg_manager, pg_pool
    ):
        # Backward compatibility: a row inserted before this migration would
        # read activation_id as NULL -- degrade gracefully to None, never
        # raise or crash.
        metadata = {
            "user_alias": "old-repo",
            "golden_repo_alias": "golden-old",
            "current_branch": "main",
            "activated_at": "2020-01-01T00:00:00+00:00",
            "last_accessed": "2020-01-01T00:00:00+00:00",
            "path": "/nfs/old-repo",
            # no activation_id key at all -- mirrors a pre-migration write
        }
        pg_manager._save_metadata("olduser", "old-repo", metadata)

        assert pg_manager.get_activation_id("olduser", "old-repo") is None


@pytest.mark.e2e
class TestActivationIdGenerationSiteWiredIntoRealActivation:
    """Proves the generation site is genuinely reached by production
    activation code, not a disconnected reader -- extracts and directly
    invokes the REAL worker function BackgroundJobManager.submit_job
    captured, using the SAME fixture conventions as this file's sibling
    test_activated_repo_manager.py."""

    @pytest.fixture
    def golden_repo_manager_mock(self):
        mock = MagicMock()
        golden_repo = GoldenRepo(
            alias="test-repo",
            repo_url="https://github.com/example/test-repo.git",
            default_branch="main",
            clone_path="/path/to/golden/test-repo",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        golden_repos_dict = {"test-repo": golden_repo}
        mock.golden_repos = golden_repos_dict
        mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
        mock.get_actual_repo_path.return_value = "/path/to/golden/test-repo"
        mock.resource_config = ServerResourceConfig()
        return mock

    @pytest.fixture
    def background_job_manager_mock(self):
        mock = MagicMock()
        mock.submit_job.return_value = "job-123"
        return mock

    @pytest.fixture
    def mock_clone_backend(self):
        backend = MagicMock()
        backend.create_clone_at_path.return_value = "/dest/path"
        return backend

    @pytest.fixture
    def activated_repo_manager(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        mock_clone_backend,
    ):
        return ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=mock_clone_backend,
        )

    @patch("subprocess.run")
    @patch("os.path.exists")
    def test_real_activation_worker_persists_a_uuid_activation_id(
        self,
        mock_exists,
        mock_subprocess,
        activated_repo_manager,
        background_job_manager_mock,
    ):
        def exists_side_effect(path):
            # Only fake "the golden repo source exists" (needed for
            # _clone_with_copy_on_write's git-repo detection); every other
            # path (activated_repos_dir, not-yet-written metadata files,
            # etc.) uses the REAL filesystem check.
            if str(path) == "/path/to/golden/test-repo":
                return True
            return _REAL_OS_PATH_EXISTS(path)

        mock_exists.side_effect = exists_side_effect

        def subprocess_side_effect(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""
            return result

        mock_subprocess.side_effect = subprocess_side_effect

        # Trigger activation -- captures the REAL worker callable + kwargs
        # BackgroundJobManager.submit_job was given (mirrors
        # test_activate_repository_success's own fixture conventions).
        activated_repo_manager.activate_repository(
            username="testuser",
            golden_repo_alias="test-repo",
            branch_name="main",  # == golden_repo.default_branch: skips the
            # branch-checkout + branch-delta-reindex subprocess blocks.
            user_alias="my-repo",
        )

        call_args = background_job_manager_mock.submit_job.call_args
        worker_fn = call_args[0][1]
        worker_kwargs = {
            k: v
            for k, v in call_args.kwargs.items()
            if k in ("username", "golden_repo_alias", "branch_name", "user_alias")
        }

        # Invoke the REAL production worker directly (no BackgroundJobManager
        # thread pool needed) -- this IS _do_activate_repository running for
        # real, proving the generation site is genuinely wired in.
        result = worker_fn(**worker_kwargs)

        assert result["success"] is True

        activation_id = activated_repo_manager.get_activation_id("testuser", "my-repo")
        assert activation_id is not None
        # A real UUID4, not a truncated/second-resolution timestamp.
        import uuid

        parsed = uuid.UUID(activation_id)
        assert str(parsed) == activation_id
