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
