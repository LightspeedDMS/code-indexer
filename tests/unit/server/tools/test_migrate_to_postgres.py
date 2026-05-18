"""
Unit tests for migrate_to_postgres.py - SQLite to PostgreSQL migration tool.

Story #418: SQLite-to-PostgreSQL Data Migration Tool

Tests cover:
- Table discovery from SQLite
- Row transformation (date, JSON, bool conversions)
- Migration order respects dependencies
- Validation compares row counts
- Idempotency (re-run doesn't duplicate)
- Table routing (main DB vs groups DB)
- Error handling (missing tables, bad JSON)

Mocking strategy: sqlite3 is used REAL (no mocking) since it is lightweight
and ships with Python. psycopg (v3) is mocked because no real PostgreSQL is
available in unit tests. This minimises "mocks are lies" surface while keeping
tests fast and hermetic.
"""

import sqlite3
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest


def _unwrap_json(value):
    """Unwrap psycopg Json wrapper if present, otherwise return as-is."""
    return getattr(value, "obj", value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_sqlite_db(tmp_path: Path, filename: str = "test.db") -> str:
    """Create a minimal SQLite database file and return its path."""
    db_path = str(tmp_path / filename)
    conn = sqlite3.connect(db_path)
    conn.close()
    return db_path


def _create_users_table(db_path: str) -> None:
    """Create a minimal users table in a SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            email TEXT,
            created_at TEXT NOT NULL,
            oidc_identity TEXT
        )"""
    )
    conn.commit()
    conn.close()


def _insert_user(db_path: str, username: str = "alice") -> None:
    """Insert a test user into the users table."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?, ?)",
        (
            username,
            "hash123",
            "admin",
            "alice@example.com",
            "2024-01-01T00:00:00+00:00",
            None,
        ),
    )
    conn.commit()
    conn.close()


def _build_mock_pg_conn(rowcount: int = 1) -> MagicMock:
    """Return a mock psycopg v3 connection."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = rowcount
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Import fixture — delays import so patching works correctly.
# ---------------------------------------------------------------------------


@pytest.fixture
def migrator_class():
    """Import and return the SqliteToPostgresMigrator class."""
    from code_indexer.server.tools.migrate_to_postgres import SqliteToPostgresMigrator

    return SqliteToPostgresMigrator


@pytest.fixture
def ordered_tables():
    """Import and return the ordered table lists."""
    from code_indexer.server.tools.migrate_to_postgres import (
        MAIN_DB_TABLES_ORDERED,
        GROUPS_DB_TABLES_ORDERED,
    )

    return MAIN_DB_TABLES_ORDERED, GROUPS_DB_TABLES_ORDERED


# ---------------------------------------------------------------------------
# TestTableDiscovery
# ---------------------------------------------------------------------------


class TestTableDiscovery:
    """Tests for _get_sqlite_tables() — real SQLite, no mocking."""

    def test_discovers_tables_from_main_db(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a SQLite database with users and global_repos tables
        When _get_sqlite_tables() is called
        Then both tables appear in the result.
        """
        db_path = _create_sqlite_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE users (username TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE global_repos (alias_name TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        m = migrator_class(db_path, str(tmp_path / "groups.db"), "pg://x")
        tables = m._get_sqlite_tables(db_path)

        assert "users" in tables
        assert "global_repos" in tables

    def test_empty_database_returns_empty_list(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given an empty SQLite database
        When _get_sqlite_tables() is called
        Then an empty list is returned.
        """
        db_path = _create_sqlite_db(tmp_path)
        m = migrator_class(db_path, str(tmp_path / "groups.db"), "pg://x")
        tables = m._get_sqlite_tables(db_path)
        assert tables == []

    def test_sqlite_internal_tables_excluded(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a SQLite database
        When _get_sqlite_tables() is called
        Then sqlite_* system tables are excluded from the result.
        """
        db_path = _create_sqlite_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE my_table (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        m = migrator_class(db_path, str(tmp_path / "groups.db"), "pg://x")
        tables = m._get_sqlite_tables(db_path)

        assert all(not t.startswith("sqlite_") for t in tables)
        assert "my_table" in tables

    def test_defaults_to_main_sqlite_path(self, tmp_path: Path, migrator_class) -> None:
        """
        Given no db_path argument
        When _get_sqlite_tables() is called
        Then it reads from self._sqlite_path.
        """
        db_path = _create_sqlite_db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE default_table (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        m = migrator_class(db_path, str(tmp_path / "groups.db"), "pg://x")
        tables = m._get_sqlite_tables()  # no arg

        assert "default_table" in tables


# ---------------------------------------------------------------------------
# TestRowTransformation
# ---------------------------------------------------------------------------


class TestRowTransformation:
    """Tests for _transform_row() — pure logic, no I/O."""

    def test_json_string_column_is_parsed(self, tmp_path: Path, migrator_class) -> None:
        """
        Given a background_jobs row with a JSON string in 'result'
        When _transform_row() is called
        Then 'result' becomes a Python dict, not a string.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        row = {
            "job_id": "abc123",
            "result": '{"status": "ok", "count": 5}',
            "status": "completed",
        }
        transformed = m._transform_row("background_jobs", row)

        result = _unwrap_json(transformed["result"])
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["count"] == 5

    def test_null_json_column_stays_none(self, tmp_path: Path, migrator_class) -> None:
        """
        Given a row where a JSON column is None
        When _transform_row() is called
        Then the column value remains None.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        row = {"job_id": "abc123", "result": None, "status": "pending"}
        transformed = m._transform_row("background_jobs", row)
        assert transformed["result"] is None

    def test_non_json_columns_pass_through_unchanged(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a row with plain text columns
        When _transform_row() is called
        Then plain text values are returned unchanged.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        row = {
            "username": "bob",
            "role": "user",
            "created_at": "2024-06-15T12:00:00+00:00",
        }
        transformed = m._transform_row("users", row)
        assert transformed["username"] == "bob"
        assert transformed["role"] == "user"
        assert transformed["created_at"] == "2024-06-15T12:00:00+00:00"

    def test_invalid_json_string_passed_through(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a JSON column containing malformed JSON
        When _transform_row() is called
        Then the original string is returned (no crash, no data loss).
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        row = {"result": "not-valid-json{{{"}
        transformed = m._transform_row("background_jobs", row)
        assert _unwrap_json(transformed["result"]) == "not-valid-json{{{"

    def test_already_parsed_dict_in_json_column_returned_as_is(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a JSON column that already contains a dict
        When _transform_row() is called
        Then the dict is returned without double-parsing.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        data = {"key": "value"}
        row = {"result": data}
        transformed = m._transform_row("background_jobs", row)
        assert _unwrap_json(transformed["result"]) == data

    def test_temporal_options_json_in_global_repos(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a global_repos row with temporal_options as a JSON string
        When _transform_row() is called
        Then temporal_options becomes a Python dict.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        row = {
            "alias_name": "my-repo-global",
            "temporal_options": '{"max_commits": 500}',
        }
        transformed = m._transform_row("global_repos", row)
        temporal = _unwrap_json(transformed["temporal_options"])
        assert isinstance(temporal, dict)
        assert temporal["max_commits"] == 500

    def test_unknown_table_passes_row_through(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a row from an unrecognised table
        When _transform_row() is called
        Then all values are passed through unchanged.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        row = {"col1": "val1", "col2": 42}
        transformed = m._transform_row("unknown_future_table", row)
        assert transformed == row


# ---------------------------------------------------------------------------
# TestMigrationOrder
# ---------------------------------------------------------------------------


class TestMigrationOrder:
    """Tests that migration order satisfies foreign-key dependencies."""

    def test_users_before_user_api_keys(self, ordered_tables) -> None:
        """users must appear before user_api_keys (FK: user_api_keys.username -> users)."""
        main, _ = ordered_tables
        assert main.index("users") < main.index("user_api_keys")

    def test_users_before_user_mcp_credentials(self, ordered_tables) -> None:
        """users must appear before user_mcp_credentials."""
        main, _ = ordered_tables
        assert main.index("users") < main.index("user_mcp_credentials")

    def test_users_before_user_oidc_identities(self, ordered_tables) -> None:
        """users must appear before user_oidc_identities."""
        main, _ = ordered_tables
        assert main.index("users") < main.index("user_oidc_identities")

    def test_repo_categories_before_golden_repos_metadata(self, ordered_tables) -> None:
        """repo_categories must appear before golden_repos_metadata (FK: category_id)."""
        main, _ = ordered_tables
        assert main.index("repo_categories") < main.index("golden_repos_metadata")

    def test_ssh_keys_before_ssh_key_hosts(self, ordered_tables) -> None:
        """ssh_keys must appear before ssh_key_hosts (FK: key_name -> ssh_keys)."""
        main, _ = ordered_tables
        assert main.index("ssh_keys") < main.index("ssh_key_hosts")

    def test_self_monitoring_scans_before_issues(self, ordered_tables) -> None:
        """self_monitoring_scans must appear before self_monitoring_issues."""
        main, _ = ordered_tables
        assert main.index("self_monitoring_scans") < main.index(
            "self_monitoring_issues"
        )

    def test_research_sessions_before_messages(self, ordered_tables) -> None:
        """research_sessions must appear before research_messages."""
        main, _ = ordered_tables
        assert main.index("research_sessions") < main.index("research_messages")

    def test_groups_before_user_group_membership(self, ordered_tables) -> None:
        """groups must appear before user_group_membership in groups DB tables."""
        _, groups = ordered_tables
        assert groups.index("groups") < groups.index("user_group_membership")

    def test_groups_before_repo_group_access(self, ordered_tables) -> None:
        """groups must appear before repo_group_access."""
        _, groups = ordered_tables
        assert groups.index("groups") < groups.index("repo_group_access")

    def test_all_main_tables_present(self, ordered_tables) -> None:
        """All expected main DB tables are in the ordered list."""
        main, _ = ordered_tables
        expected = {
            "users",
            "user_api_keys",
            "user_mcp_credentials",
            "user_oidc_identities",
            "invalidated_sessions",
            "password_change_timestamps",
            "repo_categories",
            "global_repos",
            "golden_repos_metadata",
            "background_jobs",
            "sync_jobs",
            "ci_tokens",
            "ssh_keys",
            "ssh_key_hosts",
            "description_refresh_tracking",
            "dependency_map_tracking",
            "self_monitoring_scans",
            "self_monitoring_issues",
            "research_sessions",
            "research_messages",
            "diagnostic_results",
            "wiki_cache",
            "wiki_sidebar_cache",
            "user_git_credentials",
        }
        assert expected.issubset(set(main))

    def test_all_groups_tables_present(self, ordered_tables) -> None:
        """All expected groups DB tables are in the ordered list."""
        _, groups = ordered_tables
        expected = {
            "groups",
            "user_group_membership",
            "repo_group_access",
            "audit_logs",
        }
        assert expected.issubset(set(groups))


# ---------------------------------------------------------------------------
# TestMigrateTable
# ---------------------------------------------------------------------------


class TestMigrateTable:
    """Tests for migrate_table() — uses real SQLite + mock PG connection."""

    def test_migrate_table_users_returns_row_count(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a SQLite users table with 2 rows
        When migrate_table('users') is called
        Then the return value is 2.
        """
        db_path = _create_sqlite_db(tmp_path)
        _create_users_table(db_path)
        _insert_user(db_path, "alice")
        _insert_user(db_path, "bob")

        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn, mock_cursor = _build_mock_pg_conn(rowcount=1)

        m = migrator_class(db_path, groups_path, "pg://fake")
        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            count = m.migrate_table("users")

        assert count == 2
        assert mock_conn.commit.called

    def test_migrate_table_routes_groups_table_to_groups_db(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given the 'groups' table name
        When migrate_table('groups') is called
        Then it reads from the groups DB path, not the main DB path.
        """
        main_db = _create_sqlite_db(tmp_path, "main.db")
        groups_db = _create_sqlite_db(tmp_path, "groups.db")

        # Create groups table only in groups.db
        conn = sqlite3.connect(groups_db)
        conn.execute(
            "CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "description TEXT DEFAULT '', is_default INTEGER DEFAULT 0, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO groups VALUES (1, 'default', '', 1, '2024-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        mock_conn, mock_cursor = _build_mock_pg_conn(rowcount=1)
        m = migrator_class(main_db, groups_db, "pg://fake")

        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            count = m.migrate_table("groups")

        assert count == 1

    def test_migrate_table_raises_for_unknown_table(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given an unknown table name
        When migrate_table() is called
        Then ValueError is raised with a descriptive message.
        """
        m = migrator_class("x.db", "g.db", "pg://x")
        with pytest.raises(ValueError, match="Unknown table"):
            m.migrate_table("nonexistent_future_table")

    def test_migrate_table_empty_table_returns_zero(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a SQLite users table with 0 rows
        When migrate_table('users') is called
        Then 0 is returned and no PG insert is attempted.
        """
        db_path = _create_sqlite_db(tmp_path)
        _create_users_table(db_path)
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn, mock_cursor = _build_mock_pg_conn(rowcount=0)
        m = migrator_class(db_path, groups_path, "pg://fake")

        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            count = m.migrate_table("users")

        assert count == 0
        # cursor.execute should NOT have been called for an empty table
        mock_cursor.execute.assert_not_called()

    def test_migrate_table_missing_sqlite_table_returns_zero(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given a SQLite database that does not have the 'sync_jobs' table
        When migrate_table('sync_jobs') is called
        Then 0 is returned without raising.
        """
        db_path = _create_sqlite_db(tmp_path)  # no tables
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn, mock_cursor = _build_mock_pg_conn()
        m = migrator_class(db_path, groups_path, "pg://fake")

        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            count = m.migrate_table("sync_jobs")

        assert count == 0


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for validate() — uses real SQLite + mock PG connection."""

    def test_validate_matching_counts_returns_all_match_true(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given SQLite and PG both have 2 rows in 'users'
        When validate() is called
        Then all_match is True for 'users'.
        """
        db_path = _create_sqlite_db(tmp_path)
        _create_users_table(db_path)
        _insert_user(db_path, "alice")
        _insert_user(db_path, "bob")
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        # Mock PG to return count=2 for every table
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (2,)
        mock_conn.cursor.return_value = mock_cursor

        m = migrator_class(db_path, groups_path, "pg://fake")
        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            report = m.validate()

        assert report["tables"]["users"]["sqlite_count"] == 2
        assert report["tables"]["users"]["pg_count"] == 2
        assert report["tables"]["users"]["match"] is True

    def test_validate_mismatched_counts_returns_all_match_false(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given SQLite has 1 row in 'users' but PG returns 0
        When validate() is called
        Then all_match is False.
        """
        db_path = _create_sqlite_db(tmp_path)
        _create_users_table(db_path)
        _insert_user(db_path, "alice")
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)  # PG has 0
        mock_conn.cursor.return_value = mock_cursor

        m = migrator_class(db_path, groups_path, "pg://fake")
        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            report = m.validate()

        assert report["tables"]["users"]["sqlite_count"] == 1
        assert report["tables"]["users"]["pg_count"] == 0
        assert report["tables"]["users"]["match"] is False
        assert report["all_match"] is False

    def test_validate_returns_entry_for_every_table(
        self, tmp_path: Path, migrator_class, ordered_tables
    ) -> None:
        """
        When validate() is called
        Then the report contains an entry for every table in both ordered lists.
        """
        db_path = _create_sqlite_db(tmp_path)
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.cursor.return_value = mock_cursor

        m = migrator_class(db_path, groups_path, "pg://fake")
        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            report = m.validate()

        main_tables, groups_tables = ordered_tables
        for table in main_tables + groups_tables:
            assert table in report["tables"], (
                f"Missing table '{table}' in validation report"
            )


# ---------------------------------------------------------------------------
# TestIdempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Tests that re-running migration does not duplicate rows."""

    def test_migrate_table_twice_uses_on_conflict_do_nothing(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given migrate_table() is called twice for the same table
        When the second call is made
        Then the SQL uses ON CONFLICT DO NOTHING so no duplicates occur.
        """
        db_path = _create_sqlite_db(tmp_path)
        _create_users_table(db_path)
        _insert_user(db_path, "alice")
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        executed_sqls: List[str] = []

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.rowcount = 1

        def capture_execute(sql, params=None):
            executed_sqls.append(sql)

        mock_cursor.execute.side_effect = capture_execute
        mock_conn.cursor.return_value = mock_cursor

        m = migrator_class(db_path, groups_path, "pg://fake")
        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            m.migrate_table("users")
            m.migrate_table("users")  # second call

        # Every INSERT should use ON CONFLICT DO NOTHING
        insert_sqls = [s for s in executed_sqls if "INSERT" in s.upper()]
        assert len(insert_sqls) > 0
        for sql in insert_sqls:
            assert "ON CONFLICT DO NOTHING" in sql.upper()


# ---------------------------------------------------------------------------
# TestMigrateAll
# ---------------------------------------------------------------------------


class TestMigrateAll:
    """Tests for migrate_all() — integration of the full migration flow."""

    def test_migrate_all_returns_report_with_total_rows(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given SQLite databases with data in multiple tables
        When migrate_all() is called
        Then the report includes total_rows and per-table entries.
        """
        db_path = _create_sqlite_db(tmp_path)
        _create_users_table(db_path)
        _insert_user(db_path, "alice")
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn, mock_cursor = _build_mock_pg_conn(rowcount=1)
        m = migrator_class(db_path, groups_path, "pg://fake")

        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            report = m.migrate_all()

        assert "tables" in report
        assert "total_rows" in report
        assert isinstance(report["total_rows"], int)
        assert report["total_rows"] >= 0

    def test_migrate_all_includes_entry_for_every_table(
        self, tmp_path: Path, migrator_class, ordered_tables
    ) -> None:
        """
        When migrate_all() is called
        Then the report['tables'] dict has an entry for every known table.
        """
        db_path = _create_sqlite_db(tmp_path)
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn, mock_cursor = _build_mock_pg_conn(rowcount=0)
        m = migrator_class(db_path, groups_path, "pg://fake")

        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            report = m.migrate_all()

        main_tables, groups_tables = ordered_tables
        for table in main_tables + groups_tables:
            assert table in report["tables"], f"Missing table '{table}' in report"

    def test_migrate_all_table_error_does_not_abort_others(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given one table migration raises an exception
        When migrate_all() is called
        Then other tables still complete and the failed table shows status='error:...'.
        """
        db_path = _create_sqlite_db(tmp_path)
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        call_count = [0]

        def selective_fail(table_name: str, db_path_: str) -> int:
            call_count[0] += 1
            if table_name == "users":
                raise RuntimeError("simulated PG failure")
            return 0

        m = migrator_class(db_path, groups_path, "pg://fake")
        with patch.object(m, "_migrate_table_from", side_effect=selective_fail):
            report = m.migrate_all()

        assert "error:" in report["tables"]["users"]["status"]
        assert report["tables"]["users"]["rows_migrated"] == 0
        # Other tables should still be present with status 'ok'
        assert report["tables"]["global_repos"]["status"] == "ok"

    def test_migrate_all_status_ok_for_successful_tables(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        When migrate_all() completes without errors
        Then every table entry has status='ok'.
        """
        db_path = _create_sqlite_db(tmp_path)
        groups_path = _create_sqlite_db(tmp_path, "groups.db")

        mock_conn, mock_cursor = _build_mock_pg_conn(rowcount=0)
        m = migrator_class(db_path, groups_path, "pg://fake")

        with patch.object(m, "_get_pg_connection", return_value=mock_conn):
            report = m.migrate_all()

        for table, info in report["tables"].items():
            assert info["status"] == "ok", f"Table '{table}' status: {info['status']}"


# ---------------------------------------------------------------------------
# TestPsycopgLazyImport
# ---------------------------------------------------------------------------


class TestPsycopgLazyImport:
    """Ensure psycopg import is lazy — module import must not require psycopg."""

    def test_module_imports_without_psycopg(self) -> None:
        """
        When migrate_to_postgres module is imported
        Then psycopg is NOT imported at module level.
        """
        import sys

        # Remove psycopg from sys.modules to simulate it being absent
        psycopg_backup = sys.modules.pop("psycopg", None)
        psycopg_rows_backup = sys.modules.pop("psycopg.rows", None)
        try:
            # Force re-import
            if "code_indexer.server.tools.migrate_to_postgres" in sys.modules:
                del sys.modules["code_indexer.server.tools.migrate_to_postgres"]
            # Should not raise ImportError
            import code_indexer.server.tools.migrate_to_postgres  # noqa: F401
        finally:
            if psycopg_backup is not None:
                sys.modules["psycopg"] = psycopg_backup
            if psycopg_rows_backup is not None:
                sys.modules["psycopg.rows"] = psycopg_rows_backup

    def test_get_pg_connection_raises_import_error_when_psycopg_absent(
        self, tmp_path: Path, migrator_class
    ) -> None:
        """
        Given psycopg is not installed
        When _get_pg_connection() is called
        Then ImportError is raised with a helpful message.
        """
        import sys

        m = migrator_class("x.db", "g.db", "pg://x")
        psycopg_backup = sys.modules.pop("psycopg", None)
        try:
            with patch.dict("sys.modules", {"psycopg": None}):
                with pytest.raises(ImportError, match="psycopg"):
                    m._get_pg_connection()
        finally:
            if psycopg_backup is not None:
                sys.modules["psycopg"] = psycopg_backup


# ---------------------------------------------------------------------------
# Shared helpers for new-table tests
# ---------------------------------------------------------------------------


@pytest.fixture
def new_table_constants():
    """Import constants needed for new-table assertions."""
    from code_indexer.server.tools.migrate_to_postgres import (
        MAIN_DB_TABLES_ORDERED,
        BOOLEAN_COLUMNS,
        JSON_COLUMNS,
    )

    return MAIN_DB_TABLES_ORDERED, BOOLEAN_COLUMNS, JSON_COLUMNS


def _migration_024_path() -> str:
    """Return the absolute path to migration 024_wiki_article_views.sql."""
    import os

    sql_dir = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "../../../../src/code_indexer/server/storage/postgres/migrations/sql",
        )
    )
    return os.path.join(sql_dir, "024_wiki_article_views.sql")


@pytest.fixture(scope="module")
def migration_024_sql() -> str:
    """Read migration 024 SQL once for the entire module; raises if file absent."""
    with open(_migration_024_path()) as f:
        return f.read()


# ---------------------------------------------------------------------------
# TestNewTablePresence — parametrized: all 6 missing tables
# ---------------------------------------------------------------------------


class TestNewTablePresence:
    """Verify all 6 previously-missing tables are listed in MAIN_DB_TABLES_ORDERED."""

    @pytest.mark.parametrize(
        "table",
        [
            "user_mfa",
            "user_recovery_codes",
            "activated_repos",
            "server_config",
            "dependency_map_run_history",
            "wiki_article_views",
        ],
    )
    def test_table_in_main_db_tables_ordered(
        self, table: str, new_table_constants
    ) -> None:
        """Each of the 6 new tables must appear in MAIN_DB_TABLES_ORDERED."""
        main, _, _ = new_table_constants
        assert table in main, f"'{table}' missing from MAIN_DB_TABLES_ORDERED"


# ---------------------------------------------------------------------------
# TestNewTableColumnMaps — parametrized: BOOLEAN and JSON column registrations
# ---------------------------------------------------------------------------


class TestNewTableColumnMaps:
    """Verify BOOLEAN_COLUMNS and JSON_COLUMNS entries for the new tables."""

    @pytest.mark.parametrize(
        "table,column",
        [
            ("user_mfa", "mfa_enabled"),
            ("activated_repos", "is_composite"),
            ("activated_repos", "wiki_enabled"),
        ],
    )
    def test_boolean_column_registered(
        self, table: str, column: str, new_table_constants
    ) -> None:
        """INTEGER 0/1 columns must be registered in BOOLEAN_COLUMNS."""
        _, bool_cols, _ = new_table_constants
        assert table in bool_cols
        assert column in bool_cols[table]

    @pytest.mark.parametrize(
        "table,column",
        [
            ("server_config", "config_json"),
            ("activated_repos", "metadata_json"),
            ("dependency_map_run_history", "phase_timings_json"),
        ],
    )
    def test_json_column_registered(
        self, table: str, column: str, new_table_constants
    ) -> None:
        """JSON text columns must be registered in JSON_COLUMNS."""
        _, _, json_cols = new_table_constants
        assert table in json_cols
        assert column in json_cols[table]

    def test_activated_repos_ssh_key_used_not_boolean(
        self, new_table_constants
    ) -> None:
        """activated_repos.ssh_key_used is TEXT in both SQLite and PG — not boolean."""
        _, bool_cols, _ = new_table_constants
        assert "ssh_key_used" not in bool_cols.get("activated_repos", set())


# ---------------------------------------------------------------------------
# TestNewTableOrdering — parametrized: FK dependency ordering
# ---------------------------------------------------------------------------


class TestNewTableOrdering:
    """Verify new tables appear in correct FK dependency order."""

    @pytest.mark.parametrize(
        "child,parent",
        [
            ("user_mfa", "users"),
            ("user_recovery_codes", "user_mfa"),
            ("activated_repos", "golden_repos_metadata"),
            ("dependency_map_run_history", "dependency_map_tracking"),
            ("wiki_article_views", "wiki_sidebar_cache"),
        ],
    )
    def test_child_after_parent(
        self, child: str, parent: str, new_table_constants
    ) -> None:
        """Child table must appear after its parent in MAIN_DB_TABLES_ORDERED."""
        main, _, _ = new_table_constants
        assert main.index(child) > main.index(parent), (
            f"'{child}' must come after '{parent}'"
        )


# ---------------------------------------------------------------------------
# TestNewTableRowTransformation — parametrized: _transform_row() behaviour
# ---------------------------------------------------------------------------


class TestNewTableRowTransformation:
    """Verify _transform_row() correctly converts new-table columns."""

    @pytest.mark.parametrize(
        "table,row,col,expected",
        [
            (
                "user_mfa",
                {"user_id": "u", "encrypted_secret": "s", "mfa_enabled": 1},
                "mfa_enabled",
                True,
            ),
            (
                "activated_repos",
                {
                    "id": 1,
                    "username": "a",
                    "user_alias": "r",
                    "is_composite": 0,
                    "wiki_enabled": 1,
                },
                "is_composite",
                False,
            ),
            (
                "activated_repos",
                {
                    "id": 1,
                    "username": "a",
                    "user_alias": "r",
                    "is_composite": 0,
                    "wiki_enabled": 1,
                },
                "wiki_enabled",
                True,
            ),
        ],
    )
    def test_boolean_columns_converted(
        self, table: str, row: dict, col: str, expected: bool, migrator_class
    ) -> None:
        """Integer 0/1 values in BOOLEAN_COLUMNS become Python bools."""
        m = migrator_class("x.db", "g.db", "pg://x")
        assert m._transform_row(table, row)[col] is expected

    @pytest.mark.parametrize(
        "table,row,col,expected_key",
        [
            (
                "server_config",
                {"config_key": "runtime", "config_json": '{"k": "v"}', "version": 1},
                "config_json",
                "k",
            ),
            (
                "dependency_map_run_history",
                {"run_id": 1, "phase_timings_json": '{"pass1": 1.5}'},
                "phase_timings_json",
                "pass1",
            ),
            (
                "activated_repos",
                {
                    "id": 1,
                    "username": "a",
                    "user_alias": "r",
                    "is_composite": 0,
                    "wiki_enabled": 0,
                    "metadata_json": '{"foo": "bar"}',
                },
                "metadata_json",
                "foo",
            ),
        ],
    )
    def test_json_columns_parsed(
        self, table: str, row: dict, col: str, expected_key: str, migrator_class
    ) -> None:
        """JSON string values in JSON_COLUMNS become parsed dicts."""
        m = migrator_class("x.db", "g.db", "pg://x")
        parsed = _unwrap_json(m._transform_row(table, row)[col])
        assert isinstance(parsed, dict)
        assert expected_key in parsed


# ---------------------------------------------------------------------------
# TestWikiArticleViewsMigration024 — SQL migration file checks (shared fixture)
# ---------------------------------------------------------------------------


class TestWikiArticleViewsMigration024:
    """Verify migration 024 SQL file exists and has correct content.

    All three tests depend on `migration_024_sql` so a missing file causes
    all three to fail with FileNotFoundError — no test calls the path helper
    directly.
    """

    def test_file_is_readable(self, migration_024_sql: str) -> None:
        """Fixture succeeds only if the file exists and is non-empty."""
        assert len(migration_024_sql) > 0

    def test_creates_table_if_not_exists(self, migration_024_sql: str) -> None:
        """SQL must use CREATE TABLE IF NOT EXISTS wiki_article_views."""
        assert "CREATE TABLE IF NOT EXISTS wiki_article_views" in migration_024_sql

    def test_defines_composite_primary_key(self, migration_024_sql: str) -> None:
        """SQL must define PRIMARY KEY on repo_alias and article_path."""
        assert "PRIMARY KEY" in migration_024_sql
        assert "repo_alias" in migration_024_sql
        assert "article_path" in migration_024_sql


# ---------------------------------------------------------------------------
# TestParseJsonColumn (unit tests for the free function)
# ---------------------------------------------------------------------------


class TestParseJsonColumn:
    """Tests for the _parse_json_column() utility function."""

    def test_string_json_is_parsed(self) -> None:
        from code_indexer.server.tools.migrate_to_postgres import _parse_json_column

        result = _parse_json_column('{"a": 1}')
        assert result == {"a": 1}

    def test_dict_returned_as_is(self) -> None:
        from code_indexer.server.tools.migrate_to_postgres import _parse_json_column

        data = {"a": 1}
        assert _parse_json_column(data) is data

    def test_list_returned_as_is(self) -> None:
        from code_indexer.server.tools.migrate_to_postgres import _parse_json_column

        data = [1, 2, 3]
        assert _parse_json_column(data) is data

    def test_invalid_json_string_returned_unchanged(self) -> None:
        from code_indexer.server.tools.migrate_to_postgres import _parse_json_column

        bad = "not-json"
        assert _parse_json_column(bad) == bad

    def test_none_returned_unchanged(self) -> None:
        from code_indexer.server.tools.migrate_to_postgres import _parse_json_column

        assert _parse_json_column(None) is None

    def test_integer_returned_unchanged(self) -> None:
        from code_indexer.server.tools.migrate_to_postgres import _parse_json_column

        assert _parse_json_column(42) == 42


# ---------------------------------------------------------------------------
# Helper for SQL-capturing tests
# ---------------------------------------------------------------------------


def _build_sql_capturing_pg_conn() -> tuple:
    """Return (mock_conn, executed_sqls) where executed_sqls captures every SQL executed.

    The mock conn behaves like a psycopg v3 connection. Each call to
    cursor.execute(sql, params=None) appends ``sql`` to executed_sqls so
    tests can assert on the generated SQL without a real PG database.
    """
    executed_sqls: List[str] = []

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = 1

    def capture_execute(sql, params=None):
        executed_sqls.append(sql)

    mock_cursor.execute.side_effect = capture_execute
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, executed_sqls


# ---------------------------------------------------------------------------
# TestUpsertOverwriteSemantics
# ---------------------------------------------------------------------------


class TestUpsertOverwriteSemantics:
    """Tests that _upsert_rows() uses DO UPDATE for server_config and DO NOTHING for others.

    Root cause being tested: server_config may be pre-seeded with defaults when
    the server starts in cluster mode before migration runs. The migrated SQLite
    data must take precedence, so ON CONFLICT DO NOTHING would silently discard it.
    """

    def test_upsert_overwrite_tables_constant_exists_with_server_config(self) -> None:
        """
        Given the migrate_to_postgres module
        When UPSERT_OVERWRITE_TABLES is imported
        Then it is a dict containing 'server_config' mapped to its PK column 'config_key'.
        """
        from code_indexer.server.tools.migrate_to_postgres import (
            UPSERT_OVERWRITE_TABLES,
        )

        assert isinstance(UPSERT_OVERWRITE_TABLES, dict)
        assert "server_config" in UPSERT_OVERWRITE_TABLES
        assert UPSERT_OVERWRITE_TABLES["server_config"] == "config_key"

    def test_server_config_uses_on_conflict_update_not_do_nothing(
        self, migrator_class
    ) -> None:
        """
        Given a server_config row with config_key='runtime'
        When _upsert_rows() is called for the 'server_config' table
        Then the SQL contains 'ON CONFLICT (config_key) DO UPDATE SET' (not DO NOTHING).
        """
        mock_conn, executed_sqls = _build_sql_capturing_pg_conn()

        m = migrator_class("x.db", "g.db", "pg://x")
        rows = [{"config_key": "runtime", "config_json": '{"k": "v"}', "version": 1}]
        m._upsert_rows(mock_conn, "server_config", rows)

        assert len(executed_sqls) == 1
        sql = executed_sqls[0].upper()
        assert "ON CONFLICT DO NOTHING" not in sql
        assert "ON CONFLICT (CONFIG_KEY) DO UPDATE SET" in sql
