"""
Unit tests for PostgreSQL MigrationRunner - Part 1: Discovery, checksums, table ops.

Story #416: Database Migration System with Numbered SQL Files

TDD: tests written first to drive implementation.
All tests use mocked DB connections — no real PostgreSQL required.
"""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch


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
