"""
Unit tests for PostgreSQL MigrationRunner - Part 2: Execution, status, SQL validation.

Story #416: Database Migration System with Numbered SQL Files

TDD: tests written first to drive implementation.
All tests use mocked DB connections — no real PostgreSQL required.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock


class TestApplyMigration:
    """Tests for applying a single migration file."""

    def test_apply_migration_executes_sql_content(self, tmp_path: Path) -> None:
        """
        Given a migration SQL file
        When apply_migration() is called
        Then the SQL content is executed and the migration is recorded.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_file = tmp_path / "001_test.sql"
        sql_content = "CREATE TABLE test_table (id SERIAL PRIMARY KEY);"
        sql_file.write_text(sql_content)

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        runner.apply_migration(sql_file)

        calls = mock_cursor.execute.call_args_list
        sql_calls = [str(c[0][0]) for c in calls]
        assert any(sql_content in s for s in sql_calls)

    def test_apply_migration_records_in_schema_migrations(self, tmp_path: Path) -> None:
        """
        Given a migration SQL file
        When apply_migration() is called
        Then an INSERT into schema_migrations is executed with filename and checksum.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_file = tmp_path / "001_test.sql"
        sql_file.write_text("SELECT 1;")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        runner.apply_migration(sql_file)

        calls = mock_cursor.execute.call_args_list
        insert_calls = [c for c in calls if "schema_migrations" in str(c[0][0])]
        assert len(insert_calls) >= 1
        insert_sql = str(insert_calls[0][0][0])
        assert "INSERT" in insert_sql

    def test_apply_migration_commits_transaction(self, tmp_path: Path) -> None:
        """
        Given a migration SQL file
        When apply_migration() completes successfully
        Then the transaction is committed.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_file = tmp_path / "001_test.sql"
        sql_file.write_text("SELECT 1;")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        runner.apply_migration(sql_file)

        mock_conn.commit.assert_called()

    def test_apply_migration_rolls_back_on_error(self, tmp_path: Path) -> None:
        """
        Given a migration SQL file that causes a DB error
        When apply_migration() is called
        Then the transaction is rolled back and the exception is re-raised.
        """
        import pytest

        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_file = tmp_path / "001_bad.sql"
        sql_file.write_text("INVALID SQL THAT WILL FAIL;")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("syntax error")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._conn = mock_conn

        with pytest.raises(Exception, match="syntax error"):
            runner.apply_migration(sql_file)

        mock_conn.rollback.assert_called()


class TestRunMethod:
    """Tests for the main run() method that executes all pending migrations."""

    def test_run_applies_only_pending_migrations(self, tmp_path: Path) -> None:
        """
        Given some migrations already applied and some pending
        When run() is called
        Then only pending migrations are applied.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_initial.sql").write_text("CREATE TABLE t1 (id INT);")
        (sql_dir / "002_second.sql").write_text("CREATE TABLE t2 (id INT);")
        (sql_dir / "003_third.sql").write_text("CREATE TABLE t3 (id INT);")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        applied_migrations = []

        runner.ensure_migrations_table = lambda: None
        runner.get_applied_migrations = lambda: ["001_initial.sql", "002_second.sql"]
        runner.apply_migration = lambda p: applied_migrations.append(p.name)

        count = runner.run()

        assert count == 1
        assert applied_migrations == ["003_third.sql"]

    def test_run_returns_zero_when_all_applied(self, tmp_path: Path) -> None:
        """
        Given all migrations already applied
        When run() is called
        Then it returns 0 and applies nothing.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_initial.sql").write_text("CREATE TABLE t1 (id INT);")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        applied = []

        runner.ensure_migrations_table = lambda: None
        runner.get_applied_migrations = lambda: ["001_initial.sql"]
        runner.apply_migration = lambda p: applied.append(p.name)

        count = runner.run()

        assert count == 0
        assert applied == []

    def test_run_applies_all_when_none_applied(self, tmp_path: Path) -> None:
        """
        Given no migrations applied yet
        When run() is called
        Then all discovered migrations are applied in order.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_initial.sql").write_text("CREATE TABLE t1 (id INT);")
        (sql_dir / "002_second.sql").write_text("CREATE TABLE t2 (id INT);")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir

        applied = []

        runner.ensure_migrations_table = lambda: None
        runner.get_applied_migrations = lambda: []
        runner.apply_migration = lambda p: applied.append(p.name)

        count = runner.run()

        assert count == 2
        assert applied == ["001_initial.sql", "002_second.sql"]


class TestGetStatus:
    """Tests for get_status() reporting migration state."""

    def test_get_status_returns_applied_and_pending_counts(
        self, tmp_path: Path
    ) -> None:
        """
        Given 2 applied and 1 pending migration
        When get_status() is called
        Then it returns correct applied_count, pending_count, last_applied.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_initial.sql").write_text("-- first")
        (sql_dir / "002_second.sql").write_text("-- second")
        (sql_dir / "003_third.sql").write_text("-- third")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir
        runner.get_applied_migrations = lambda: ["001_initial.sql", "002_second.sql"]

        status = runner.get_status()

        assert status["applied_count"] == 2
        assert status["pending_count"] == 1
        assert status["last_applied"] == "002_second.sql"

    def test_get_status_last_applied_is_none_when_nothing_applied(
        self, tmp_path: Path
    ) -> None:
        """
        Given no migrations applied yet
        When get_status() is called
        Then last_applied is None.
        """
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        (sql_dir / "001_initial.sql").write_text("-- first")

        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = sql_dir
        runner.get_applied_migrations = lambda: []

        status = runner.get_status()

        assert status["applied_count"] == 0
        assert status["pending_count"] == 1
        assert status["last_applied"] is None


class TestInitialSchemaSql:
    """Tests that 001_initial_schema.sql is valid and contains required tables."""

    def _get_sql_file_path(self) -> Path:
        """Return path to 001_initial_schema.sql."""
        return (
            Path(__file__).parent.parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "storage"
            / "postgres"
            / "migrations"
            / "sql"
            / "001_initial_schema.sql"
        )

    def test_initial_schema_sql_file_exists(self) -> None:
        """
        Given the project source tree
        When looking for 001_initial_schema.sql
        Then it exists at the expected path.
        """
        sql_path = self._get_sql_file_path()
        assert sql_path.exists(), f"Expected SQL file at {sql_path}"

    def test_initial_schema_sql_is_parseable(self) -> None:
        """
        Given 001_initial_schema.sql
        When its content is read and parsed
        Then it contains valid SQL structure (contains CREATE TABLE statements).
        """
        sql_path = self._get_sql_file_path()
        content = sql_path.read_text()

        assert len(content.strip()) > 0
        assert "CREATE TABLE" in content.upper()

    def test_initial_schema_contains_schema_migrations_table(self) -> None:
        """
        Given 001_initial_schema.sql
        When its content is read
        Then it contains the schema_migrations table definition.
        """
        sql_path = self._get_sql_file_path()
        content = sql_path.read_text().upper()

        assert "SCHEMA_MIGRATIONS" in content

    def test_initial_schema_contains_core_tables(self) -> None:
        """
        Given 001_initial_schema.sql
        When its content is read
        Then it contains all core server tables.
        """
        sql_path = self._get_sql_file_path()
        content = sql_path.read_text().upper()

        required_tables = [
            "USERS",
            "GLOBAL_REPOS",
            "GOLDEN_REPOS_METADATA",
            "BACKGROUND_JOBS",
            "SSH_KEYS",
            "CI_TOKENS",
            "SYNC_JOBS",
        ]
        for table in required_tables:
            assert table in content, f"Expected table {table} in initial schema SQL"

    def test_initial_schema_uses_timestamptz_not_text_for_dates(self) -> None:
        """
        Given 001_initial_schema.sql is for PostgreSQL
        When its content is read
        Then date/time columns use TIMESTAMPTZ not TEXT.
        """
        sql_path = self._get_sql_file_path()
        content = sql_path.read_text().upper()

        assert (
            "TIMESTAMPTZ" in content
        ), "PostgreSQL schema should use TIMESTAMPTZ for timestamp columns"

    def test_initial_schema_uses_jsonb_for_json_columns(self) -> None:
        """
        Given 001_initial_schema.sql is for PostgreSQL
        When its content is read
        Then JSON columns use JSONB not TEXT.
        """
        sql_path = self._get_sql_file_path()
        content = sql_path.read_text().upper()

        assert "JSONB" in content, "PostgreSQL schema should use JSONB for JSON columns"

    def test_initial_schema_uses_serial_for_autoincrement(self) -> None:
        """
        Given 001_initial_schema.sql is for PostgreSQL
        When its content is read
        Then auto-increment columns use SERIAL (not AUTOINCREMENT which is SQLite).
        """
        sql_path = self._get_sql_file_path()
        content = sql_path.read_text().upper()

        assert (
            "SERIAL" in content
        ), "PostgreSQL schema should use SERIAL for auto-increment columns"
        assert (
            "AUTOINCREMENT" not in content
        ), "PostgreSQL schema must not use SQLite AUTOINCREMENT keyword"


class TestModuleEntryPoint:
    """Tests for the __main__ CLI entry point."""

    def test_module_is_runnable_as_main(self) -> None:
        """
        Given the runner module
        When checking if it supports __main__ entry
        Then it has an if __name__ == '__main__' block.
        """
        import importlib.util

        spec = importlib.util.find_spec(
            "code_indexer.server.storage.postgres.migrations.runner"
        )
        assert spec is not None, "runner module must be importable"

        runner_path = Path(spec.origin)
        content = runner_path.read_text()
        assert (
            '__name__ == "__main__"' in content or "if __name__ ==" in content
        ), "runner.py must have a __main__ entry point"

    def test_connection_string_argument_is_required(self) -> None:
        """
        Given the runner CLI
        When invoked without --connection-string
        Then it exits with an error (argument is required).
        """
        import subprocess
        import sys

        src_path = str(Path(__file__).parent.parent.parent.parent.parent.parent / "src")
        env = {**os.environ, "PYTHONPATH": src_path}

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "code_indexer.server.storage.postgres.migrations.runner",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
