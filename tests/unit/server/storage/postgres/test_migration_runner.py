"""
Unit tests for PostgreSQL MigrationRunner - Part 1: Discovery, checksums, table ops.

Story #416: Database Migration System with Numbered SQL Files
Story #1164: PG Migration Concurrent Startup Safety (advisory lock)

TDD: tests written first to drive implementation.
All tests use mocked DB connections — no real PostgreSQL required.
Live-PG tests are gated by TEST_POSTGRES_DSN env var (skip when absent).
"""

import hashlib
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestMigrationDiscovery:
    """Tests for discovering SQL migration files in the sql/ directory."""

    def test_discover_finds_sql_files_in_order(self, tmp_path: Path) -> None:
        """
        Given SQL files in a sql/ directory with numeric prefixes
        When discover_migrations() is called
        Then files are returned sorted numerically by prefix.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "003_add_indexes.sql").write_text("CREATE INDEX ...")
        (sql_dir / "001_initial_schema.sql").write_text("CREATE TABLE ...")
        (sql_dir / "002_add_users.sql").write_text("ALTER TABLE ...")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        migrations = runner.discover_migrations()

        assert len(migrations) == 3
        assert migrations[0].name == "001_initial_schema.sql"
        assert migrations[1].name == "002_add_users.sql"
        assert migrations[2].name == "003_add_indexes.sql"

    def test_discover_ignores_non_sql_files(self, tmp_path: Path) -> None:
        """
        Given a sql/ directory containing non-SQL files
        When discover_migrations() is called
        Then only .sql files are returned.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_schema.sql").write_text("CREATE TABLE ...")
        (sql_dir / "README.md").write_text("# Migrations")
        (sql_dir / "notes.txt").write_text("some notes")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        migrations = runner.discover_migrations()

        assert len(migrations) == 1
        assert migrations[0].name == "001_schema.sql"

    def test_discover_returns_empty_list_when_no_files(self, tmp_path: Path) -> None:
        """
        Given an empty sql/ directory
        When discover_migrations() is called
        Then an empty list is returned.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        migrations = runner.discover_migrations()

        assert migrations == []

    def test_discover_sorts_numerically_not_lexicographically(
        self, tmp_path: Path
    ) -> None:
        """
        Given files numbered 1, 9, 10 (which sort differently lexicographically)
        When discover_migrations() is called
        Then they are sorted 001, 009, 010 (numerically correct).
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "010_third.sql").write_text("-- third")
        (sql_dir / "001_first.sql").write_text("-- first")
        (sql_dir / "009_second.sql").write_text("-- second")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        migrations = runner.discover_migrations()

        assert migrations[0].name == "001_first.sql"
        assert migrations[1].name == "009_second.sql"
        assert migrations[2].name == "010_third.sql"


class TestChecksumCalculation:
    """Tests for migration file checksum calculation."""

    def test_checksum_is_md5_of_file_content(self, tmp_path: Path) -> None:
        """
        Given a SQL file with known content
        When _calculate_checksum() is called
        Then it returns the MD5 hex digest of the file content.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_file = tmp_path / "001_test.sql"
        content = "CREATE TABLE test (id SERIAL PRIMARY KEY);"
        sql_file.write_text(content)

        runner = MigrationRunner.__new__(MigrationRunner)
        checksum = runner._calculate_checksum(sql_file)

        expected = hashlib.md5(content.encode("utf-8")).hexdigest()
        assert checksum == expected

    def test_checksum_is_deterministic(self, tmp_path: Path) -> None:
        """
        Given the same file content
        When _calculate_checksum() is called twice
        Then it returns the same checksum both times.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_file = tmp_path / "001_test.sql"
        sql_file.write_text("SELECT 1;")

        runner = MigrationRunner.__new__(MigrationRunner)
        checksum1 = runner._calculate_checksum(sql_file)
        checksum2 = runner._calculate_checksum(sql_file)

        assert checksum1 == checksum2

    def test_different_content_produces_different_checksum(
        self, tmp_path: Path
    ) -> None:
        """
        Given two files with different content
        When _calculate_checksum() is called on each
        Then different checksums are returned.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        file_a = tmp_path / "file_a.sql"
        file_b = tmp_path / "file_b.sql"
        file_a.write_text("CREATE TABLE a (id INT);")
        file_b.write_text("CREATE TABLE b (id INT);")

        runner = MigrationRunner.__new__(MigrationRunner)
        assert runner._calculate_checksum(file_a) != runner._calculate_checksum(file_b)


class TestMigrationsTableCreation:
    """Tests for schema_migrations table creation (idempotent)."""

    def test_ensure_migrations_table_executes_create_if_not_exists(self) -> None:
        """
        Given a mock database connection
        When ensure_migrations_table() is called
        Then it executes CREATE TABLE IF NOT EXISTS schema_migrations.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        runner.ensure_migrations_table()

        mock_cursor.execute.assert_called_once()
        sql_called = mock_cursor.execute.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sql_called

    def test_ensure_migrations_table_includes_required_columns(self) -> None:
        """
        Given a mock database connection
        When ensure_migrations_table() is called
        Then the SQL includes id, filename, applied_at, checksum columns.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        runner.ensure_migrations_table()

        sql_called = mock_cursor.execute.call_args[0][0]
        assert "id" in sql_called
        assert "filename" in sql_called
        assert "applied_at" in sql_called
        assert "checksum" in sql_called


class TestGetAppliedMigrations:
    """Tests for retrieving already-applied migrations from schema_migrations table."""

    def test_get_applied_returns_list_of_filenames(self) -> None:
        """
        Given a mock DB that returns rows from schema_migrations
        When get_applied_migrations() is called
        Then it returns a list of filename strings.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("001_initial_schema.sql",),
            ("002_add_indexes.sql",),
        ]
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        result = runner.get_applied_migrations()

        assert result == ["001_initial_schema.sql", "002_add_indexes.sql"]

    def test_get_applied_returns_empty_list_when_no_migrations(self) -> None:
        """
        Given a mock DB with no rows in schema_migrations
        When get_applied_migrations() is called
        Then an empty list is returned.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        result = runner.get_applied_migrations()

        assert result == []


class TestMigrationRunnerInit:
    """Tests for MigrationRunner initialization with connection string."""

    def test_init_stores_connection_string(self) -> None:
        """
        Given a PostgreSQL connection string
        When MigrationRunner is instantiated
        Then the connection string is stored (connection is lazy).
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        conn_str = "postgresql://user:pass@localhost/testdb"

        with patch(
            "code_indexer.server.storage.postgres.migrations.runner.psycopg"
        ) as mock_psycopg:
            mock_psycopg.connect.return_value = MagicMock()
            runner = MigrationRunner(conn_str)

        assert runner._connection_string == conn_str

    def test_init_connects_to_database(self) -> None:
        """
        Given a PostgreSQL connection string
        When MigrationRunner is instantiated
        Then it establishes a connection via psycopg.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        conn_str = "postgresql://user:pass@localhost/testdb"

        with patch(
            "code_indexer.server.storage.postgres.migrations.runner.psycopg"
        ) as mock_psycopg:
            mock_conn = MagicMock()
            mock_psycopg.connect.return_value = mock_conn
            _runner = MigrationRunner(conn_str)  # noqa: F841

        mock_psycopg.connect.assert_called_once_with(conn_str)

    def test_sql_dir_points_to_sql_subdirectory(self) -> None:
        """
        Given MigrationRunner is instantiated
        When _sql_dir is inspected
        Then it points to the sql/ subdirectory of the migrations package.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        conn_str = "postgresql://user:pass@localhost/testdb"

        with patch(
            "code_indexer.server.storage.postgres.migrations.runner.psycopg"
        ) as mock_psycopg:
            mock_psycopg.connect.return_value = MagicMock()
            runner = MigrationRunner(conn_str)

        assert runner._sql_dir.name == "sql"


# ---------------------------------------------------------------------------
# Story #1164: Advisory Lock Tests (mocked psycopg — run locally)
# ---------------------------------------------------------------------------

# Expected lock key derived from sha256("cidx_migrations")[:8] big-endian signed.
# Verify: int.from_bytes(hashlib.sha256(b"cidx_migrations").digest()[:8], "big", signed=True)
_EXPECTED_LOCK_KEY = 8835134184625913288


def _build_mock_conn():
    """
    Unified helper: return (mock_conn, mock_cursor) with context-manager wiring.
    mock_cursor.fetchall returns [] (no applied migrations).
    """
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []

    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cm.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cm
    return mock_conn, mock_cursor


def _make_runner_with_mock_conn(mock_conn, sql_dir):
    """Build a MigrationRunner.__new__ instance wired with a mocked connection."""
    from code_indexer.server.storage.postgres.migrations.runner import MigrationRunner

    runner = MigrationRunner.__new__(MigrationRunner)
    runner._conn = mock_conn
    runner._sql_dir = sql_dir
    return runner


class TestAdvisoryLockStructural:
    """
    Story #1164 AC1: pg_advisory_lock is acquired BEFORE ensure_migrations_table
    and pg_advisory_unlock is called in finally even when an inner step raises.
    Uses mocked psycopg — no live PostgreSQL required.
    """

    def test_advisory_lock_acquired_before_ensure_migrations_table(
        self, tmp_path
    ) -> None:
        """
        Given a MigrationRunner with a mocked connection and no pending migrations
        When run() is called
        Then pg_advisory_lock(<key>) executes BEFORE ensure_migrations_table's CREATE
        and pg_advisory_unlock(<key>) executes after (in the finally block).
        """
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        mock_conn, mock_cursor = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)

        runner.run()

        all_calls = mock_cursor.execute.call_args_list
        lock_indices = [
            i for i, c in enumerate(all_calls) if "pg_advisory_lock" in str(c)
        ]
        unlock_indices = [
            i for i, c in enumerate(all_calls) if "pg_advisory_unlock" in str(c)
        ]
        ensure_indices = [
            i
            for i, c in enumerate(all_calls)
            if "CREATE TABLE IF NOT EXISTS schema_migrations" in str(c)
        ]

        assert lock_indices, "pg_advisory_lock must be called"
        assert unlock_indices, "pg_advisory_unlock must be called"
        assert ensure_indices, "ensure_migrations_table CREATE TABLE must be called"
        assert lock_indices[0] < ensure_indices[0], (
            "pg_advisory_lock must execute BEFORE ensure_migrations_table"
        )
        assert unlock_indices[0] > ensure_indices[0], (
            "pg_advisory_unlock must execute AFTER ensure_migrations_table"
        )

    def test_advisory_lock_and_unlock_use_same_key(self, tmp_path) -> None:
        """
        Both pg_advisory_lock and pg_advisory_unlock must use the SAME key constant.
        """
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        mock_conn, mock_cursor = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)

        runner.run()

        all_calls = mock_cursor.execute.call_args_list
        lock_calls = [c for c in all_calls if "pg_advisory_lock" in str(c)]
        unlock_calls = [c for c in all_calls if "pg_advisory_unlock" in str(c)]

        assert lock_calls, "pg_advisory_lock must be called"
        assert unlock_calls, "pg_advisory_unlock must be called"

        lock_key = lock_calls[0][0][1][0]
        unlock_key = unlock_calls[0][0][1][0]

        assert lock_key == unlock_key, (
            "pg_advisory_lock and pg_advisory_unlock must use the same key"
        )
        assert lock_key == _EXPECTED_LOCK_KEY, (
            f"Lock key must be {_EXPECTED_LOCK_KEY}, got {lock_key}"
        )

    def test_advisory_unlock_called_even_when_apply_migration_raises(
        self, tmp_path
    ) -> None:
        """
        Even when apply_migration raises, pg_advisory_unlock is called (finally)
        and the exception propagates unchanged.
        """
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_test.sql").write_text("CREATE TABLE test_tbl (id INT);")

        mock_conn, mock_cursor = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)
        runner.apply_migration = MagicMock(side_effect=RuntimeError("DB write failed"))

        with pytest.raises(RuntimeError, match="DB write failed"):
            runner.run()

        all_calls = mock_cursor.execute.call_args_list
        unlock_calls = [c for c in all_calls if "pg_advisory_unlock" in str(c)]
        assert unlock_calls, (
            "pg_advisory_unlock must be called even when apply_migration raises"
        )

    def test_advisory_lock_uses_parameterized_query_not_fstring(self, tmp_path) -> None:
        """
        pg_advisory_lock must use a %s bind param — NOT an f-string integer literal.
        """
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        mock_conn, mock_cursor = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)

        runner.run()

        all_calls = mock_cursor.execute.call_args_list
        lock_calls = [c for c in all_calls if "pg_advisory_lock" in str(c)]
        assert lock_calls, "pg_advisory_lock must be called"

        lock_sql = lock_calls[0][0][0]
        assert "%s" in lock_sql, (
            "pg_advisory_lock query must use %s bind param, not f-string interpolation"
        )
        assert str(_EXPECTED_LOCK_KEY) not in lock_sql, (
            "Lock key integer must not be interpolated directly into the SQL string"
        )


class TestAdvisoryLockIntReturn:
    """
    Story #1164 AC2: run() return value (applied count) is preserved
    under the advisory lock wrapper.
    """

    def test_run_returns_count_of_pending_migrations_applied(self, tmp_path) -> None:
        """
        Given 2 pending migration files and no applied migrations,
        run() returns exactly 2.
        """
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_alpha.sql").write_text("SELECT 1;")
        (sql_dir / "002_beta.sql").write_text("SELECT 2;")

        mock_conn, _ = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)
        runner.apply_migration = MagicMock()

        result = runner.run()

        assert result == 2, (
            f"run() must return 2 (count of pending applied), got {result}"
        )

    def test_run_returns_zero_when_no_pending_migrations(self, tmp_path) -> None:
        """Given no migration files, run() returns 0."""
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()

        mock_conn, _ = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)

        result = runner.run()

        assert result == 0, (
            f"run() must return 0 when no pending migrations, got {result}"
        )

    def test_run_returns_int_type(self, tmp_path) -> None:
        """run() must return an int (not None, not bool, not str)."""
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()

        mock_conn, _ = _build_mock_conn()
        runner = _make_runner_with_mock_conn(mock_conn, sql_dir)

        result = runner.run()

        assert isinstance(result, int), f"run() must return int, got {type(result)}"


class TestLockKeyStability:
    """
    Story #1164 AC3: _MIGRATION_ADVISORY_LOCK_KEY is a fixed, stable constant
    within signed-64-bit range, identical on every node.
    """

    def test_lock_key_has_expected_value(self) -> None:
        """
        Hard-coded assertion: _MIGRATION_ADVISORY_LOCK_KEY == 8835134184625913288.
        Proves every node uses the same key.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            _MIGRATION_ADVISORY_LOCK_KEY,
        )

        assert _MIGRATION_ADVISORY_LOCK_KEY == _EXPECTED_LOCK_KEY, (
            f"Lock key must be {_EXPECTED_LOCK_KEY}, got {_MIGRATION_ADVISORY_LOCK_KEY}"
        )

    def test_lock_key_is_within_signed_64bit_range(self) -> None:
        """_MIGRATION_ADVISORY_LOCK_KEY must fit in PostgreSQL bigint (signed 64-bit)."""
        from code_indexer.server.storage.postgres.migrations.runner import (
            _MIGRATION_ADVISORY_LOCK_KEY,
        )

        assert -(2**63) <= _MIGRATION_ADVISORY_LOCK_KEY <= (2**63 - 1), (
            "_MIGRATION_ADVISORY_LOCK_KEY must be within signed 64-bit range"
        )

    def test_lock_key_is_int(self) -> None:
        """_MIGRATION_ADVISORY_LOCK_KEY must be a Python int."""
        from code_indexer.server.storage.postgres.migrations.runner import (
            _MIGRATION_ADVISORY_LOCK_KEY,
        )

        assert isinstance(_MIGRATION_ADVISORY_LOCK_KEY, int)

    def test_lock_key_matches_sha256_derivation(self) -> None:
        """
        _MIGRATION_ADVISORY_LOCK_KEY must reproducibly equal
        int.from_bytes(sha256(b'cidx_migrations').digest()[:8], 'big', signed=True).
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            _MIGRATION_ADVISORY_LOCK_KEY,
        )

        expected = int.from_bytes(
            hashlib.sha256(b"cidx_migrations").digest()[:8], "big", signed=True
        )
        assert _MIGRATION_ADVISORY_LOCK_KEY == expected


# ---------------------------------------------------------------------------
# Story #1164: Live PostgreSQL tests (gated — skip without TEST_POSTGRES_DSN)
# ---------------------------------------------------------------------------

HAS_PSYCOPG_FOR_RUNNER = False
try:
    import psycopg as _psycopg_check  # noqa: F401

    HAS_PSYCOPG_FOR_RUNNER = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_dsn_for_runner():
    """Module-scoped DSN string for live-PG runner tests. Skips if unavailable."""
    if not HAS_PSYCOPG_FOR_RUNNER:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        import psycopg

        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def isolated_schema(pg_dsn_for_runner):
    """
    Drop and recreate schema_migrations before each live-PG test for isolation.
    Uses context manager to guarantee connection cleanup.
    """
    import psycopg

    with psycopg.connect(pg_dsn_for_runner, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
    yield pg_dsn_for_runner
    with psycopg.connect(pg_dsn_for_runner, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS schema_migrations")


@pytest.mark.skipif(not HAS_PSYCOPG_FOR_RUNNER, reason="psycopg not available")
class TestAdvisoryLockConcurrentLivePG:
    """
    Story #1164 AC4 & AC5: Concurrent-startup exactly-once migration apply.
    LIVE PostgreSQL tests — skip when TEST_POSTGRES_DSN is absent.
    """

    def test_concurrent_run_applies_migration_exactly_once(
        self, isolated_schema, tmp_path
    ) -> None:
        """
        Given 2 MigrationRunner instances sharing one PostgreSQL DB + 1 pending migration,
        when both call run() concurrently, the migration is applied exactly once,
        both callers return without raising, and counts sum to 1 (one gets 1, other 0).
        """
        import psycopg

        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_concurrent_test.sql").write_text(
            "CREATE TABLE IF NOT EXISTS concurrent_test_tbl (id INT);"
        )

        results = []
        errors = []
        lock = threading.Lock()

        def run_runner():
            runner = None
            try:
                runner = MigrationRunner(isolated_schema)
                runner._sql_dir = sql_dir
                count = runner.run()
                with lock:
                    results.append(count)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                if runner is not None:
                    runner.close()

        t1 = threading.Thread(target=run_runner)
        t2 = threading.Thread(target=run_runner)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"Concurrent run() raised exceptions: {errors}"
        assert len(results) == 2, (
            f"Both runners must complete, got {len(results)} results"
        )
        assert sum(results) == 1, (
            f"Total applied must be 1 (one applies, one sees 0), got sum={sum(results)}"
        )
        assert sorted(results) == [0, 1], (
            f"One runner applies 1, other applies 0; got {sorted(results)}"
        )

        with psycopg.connect(isolated_schema) as conn:
            rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
        assert len(rows) == 1, (
            f"schema_migrations must have exactly 1 row, got {len(rows)}"
        )
        assert rows[0][0] == "001_concurrent_test.sql"

    def test_concurrent_run_no_pending_returns_zero_for_all(
        self, isolated_schema, tmp_path
    ) -> None:
        """
        Story #1164 AC5: With no pending migrations, all concurrent run() calls
        return 0 without raising.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()

        results = []
        errors = []
        lock = threading.Lock()

        def run_runner():
            runner = None
            try:
                runner = MigrationRunner(isolated_schema)
                runner._sql_dir = sql_dir
                count = runner.run()
                with lock:
                    results.append(count)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                if runner is not None:
                    runner.close()

        threads = [threading.Thread(target=run_runner) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, (
            f"Concurrent run() with no pending raised exceptions: {errors}"
        )
        assert all(r == 0 for r in results), (
            f"All runners must return 0 when no pending migrations, got {results}"
        )


class TestSQLitePathUntouched:
    """
    Story #1164 AC6: The SQLite initialization path must NOT call pg_advisory_lock.
    Verified by source-level inspection of database_manager.py.
    """

    def test_database_manager_sqlite_has_no_advisory_lock_call(self) -> None:
        """
        database_manager.py (SQLite path) must NOT contain any reference to
        pg_advisory_lock or pg_advisory_unlock — the advisory lock is isolated
        to the PostgreSQL MigrationRunner only.
        """
        import inspect

        from code_indexer.server.storage import database_manager

        source = inspect.getsource(database_manager)
        assert "pg_advisory_lock" not in source, (
            "database_manager.py (SQLite path) must NOT reference pg_advisory_lock"
        )
        assert "pg_advisory_unlock" not in source, (
            "database_manager.py (SQLite path) must NOT reference pg_advisory_unlock"
        )
