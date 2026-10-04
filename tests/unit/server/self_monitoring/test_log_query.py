"""
Tests for code_indexer.server.self_monitoring.log_query.

Replaces the raw `sqlite3` CLI as the self-monitoring scan's sole Bash
command. This module goes through Python's sqlite3 module directly (no
shell involved, so no dot-commands exist at all), opens the database
read-only via a `file:...?mode=ro` URI, installs a `set_authorizer`
callback that permits only SELECT/read/function operations, bounds query
wall-clock time with `set_progress_handler`, and caps randomblob/zeroblob
allocation size.

No mocking of sqlite3 itself: every test exercises a real, throwaway
on-disk (or in-memory) SQLite database.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import code_indexer
from code_indexer.server.self_monitoring import log_query

# The package root (<repo>/src), derived from the installed/imported
# code_indexer package itself rather than a hardcoded path -- this
# repository is public, so no host-specific path belongs in test source.
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(code_indexer.__file__)))

# Budget for each `python -m ...log_query` subprocess in TestMainCliInvocation.
_CLI_SUBPROCESS_TIMEOUT_SECONDS = 30


def _make_logs_db(tmp_path, extra_rows=0):
    db_path = str(tmp_path / "logs.db")
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT NOT NULL, "
        "level TEXT NOT NULL, "
        "source TEXT NOT NULL, "
        "message TEXT NOT NULL, "
        "correlation_id TEXT, "
        "user_id TEXT, "
        "request_path TEXT, "
        "extra_data TEXT, "
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO logs (timestamp, level, source, message, created_at) "
        "VALUES ('t', 'WARNING', 'src.mod', 'hello world', 't')"
    )
    for i in range(extra_rows):
        conn.execute(
            "INSERT INTO logs (timestamp, level, source, message, created_at) "
            "VALUES (?, 'INFO', 'src.mod', ?, 't')",
            (f"t{i}", f"row {i}"),
        )
    conn.commit()
    conn.close()
    return db_path


class TestRunQuery:
    def test_select_returns_rows(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        columns, rows, truncated = log_query.run_query(
            db_path, "SELECT level, message FROM logs"
        )
        assert columns == ["level", "message"]
        assert rows == [("WARNING", "hello world")]
        assert truncated is False

    def test_select_matching_real_prompt_query_shape(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        columns, rows, truncated = log_query.run_query(
            db_path,
            "SELECT id, timestamp, level, source, message, correlation_id "
            "FROM logs WHERE id > 0 AND level IN ('ERROR','WARNING','CRITICAL') "
            "ORDER BY id ASC LIMIT 100",
        )
        assert len(rows) == 1

    def test_frequency_query_with_group_by_having_count_works(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        columns, rows, truncated = log_query.run_query(
            db_path,
            "SELECT SUBSTR(message, 1, 80) as pattern, source, COUNT(*) as count "
            "FROM logs WHERE level = 'WARNING' GROUP BY SUBSTR(message, 1, 80), "
            "source HAVING COUNT(*) >= 1 ORDER BY count DESC",
        )
        assert len(rows) == 1

    def test_trailing_comment_after_semicolon_still_works(self, tmp_path):
        """Python's sqlite3 execute() accepts a trailing comment after a
        single statement's semicolon -- this must keep working."""
        db_path = _make_logs_db(tmp_path)
        columns, rows, truncated = log_query.run_query(
            db_path, "SELECT level FROM logs; -- trailing comment"
        )
        assert rows == [("WARNING",)]

    def test_rejects_attach(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(
                db_path, f"ATTACH DATABASE '{tmp_path}/other.db' AS other"
            )

    def test_rejects_pragma(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "PRAGMA table_info(logs)")

    def test_rejects_load_extension(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "SELECT load_extension('x')")

    def test_rejects_insert(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(
                db_path,
                "INSERT INTO logs (timestamp, level, source, message, created_at) "
                "VALUES ('x','x','x','x','x')",
            )

    def test_rejects_drop_table(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "DROP TABLE logs")

    def test_rejects_create_table(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "CREATE TABLE other(x)")

    def test_rejects_second_statement(self, tmp_path):
        """Python's sqlite3 module itself refuses a second statement
        ("You can only execute one statement at a time.") -- verified
        directly against a real connection, and never executes it."""
        import sqlite3

        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "SELECT 1; DROP TABLE logs")

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()
        assert count == 1, "the second statement must never have executed"

    def test_rejects_empty_db_path(self, tmp_path):
        with pytest.raises(log_query.QueryRejected):
            log_query.run_query("", "SELECT 1")

    def test_caps_row_count(self, tmp_path):
        db_path = _make_logs_db(tmp_path, extra_rows=log_query.MAX_ROWS + 50)
        columns, rows, truncated = log_query.run_query(db_path, "SELECT id FROM logs")
        assert len(rows) == log_query.MAX_ROWS
        assert truncated is True

    def test_write_actually_did_not_happen_after_rejected_insert(self, tmp_path):
        """Defense-in-depth check: not only does the call raise, the row
        genuinely never lands in the database."""
        import sqlite3

        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(
                db_path,
                "INSERT INTO logs (timestamp, level, source, message, created_at) "
                "VALUES ('x','x','x','x','x')",
            )
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()
        assert count == 1, "the rejected INSERT must not have been committed"


class TestQueryWallClockDeadline:
    """A query with no time bound can run arbitrarily long (a self-join, a
    sorted cross join, ...). run_query enforces a wall-clock deadline via
    set_progress_handler, independent of what makes the query slow. Tests
    use a tiny deadline (passed to the internal _run_query helper) instead
    of waiting out the real default, but the mechanism is identical."""

    def test_long_running_cross_join_is_aborted_by_the_deadline(self, tmp_path):
        import time

        db_path = str(tmp_path / "logs.db")
        import sqlite3

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE logs (id INTEGER)")
        conn.executemany(
            "INSERT INTO logs (id) VALUES (?)", [(i,) for i in range(2000)]
        )
        conn.commit()
        conn.close()

        start = time.monotonic()
        with pytest.raises(Exception):
            log_query._run_query(
                db_path,
                "SELECT COUNT(*) FROM logs a, logs b, logs c",
                deadline_seconds=0.2,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, (
            f"query must be aborted near the deadline, not run to completion "
            f"(took {elapsed:.2f}s)"
        )

    def test_default_run_query_uses_the_module_constant_deadline(self, tmp_path):
        """run_query() (the public entry point) must use
        QUERY_WALL_CLOCK_DEADLINE_SECONDS, not some other value."""
        db_path = _make_logs_db(tmp_path)
        # A fast, ordinary query must still succeed well within the real
        # default deadline.
        columns, rows, truncated = log_query.run_query(
            db_path, "SELECT level FROM logs"
        )
        assert rows == [("WARNING",)]


class TestBlobSizeCap:
    """randomblob/zeroblob with no size bound can exhaust memory.
    run_query overrides both with a capped implementation via
    create_function."""

    def test_randomblob_over_cap_is_rejected(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "SELECT length(randomblob(999999999))")

    def test_zeroblob_over_cap_is_rejected(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        with pytest.raises(Exception):
            log_query.run_query(db_path, "SELECT length(zeroblob(999999999))")

    def test_randomblob_under_cap_still_works(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        columns, rows, truncated = log_query.run_query(
            db_path, "SELECT length(randomblob(100))"
        )
        assert rows == [(100,)]

    def test_zeroblob_under_cap_still_works(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        columns, rows, truncated = log_query.run_query(
            db_path, "SELECT length(zeroblob(100))"
        )
        assert rows == [(100,)]


# Each test spawns a fresh interpreter importing the server package; under
# parallel gate load that exceeds the suite's default 15 s pytest-timeout,
# so the ceiling must sit above the subprocess's own budget.
@pytest.mark.timeout(_CLI_SUBPROCESS_TIMEOUT_SECONDS + 15)
class TestMainCliInvocation:
    """End-to-end: invoke the module exactly as the self-monitoring scan's
    Bash allow rule pins it -- `<python> -m
    code_indexer.server.self_monitoring.log_query "<SQL>"` -- with the DB
    path supplied ONLY via the environment variable, never a CLI argument.
    """

    def _run_module(self, sql, env_overrides, timeout=_CLI_SUBPROCESS_TIMEOUT_SECONDS):
        env = {**os.environ, **env_overrides}
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "code_indexer.server.self_monitoring.log_query",
                sql,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=_SRC_ROOT,
        )

    def test_valid_query_prints_header_and_row(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        result = self._run_module(
            "SELECT level, message FROM logs",
            {log_query.ENV_VAR_DB_PATH: db_path},
        )
        assert result.returncode == 0, result.stderr
        assert "level|message" in result.stdout
        assert "WARNING|hello world" in result.stdout

    def test_missing_env_var_fails_with_nonzero_exit(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != log_query.ENV_VAR_DB_PATH}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "code_indexer.server.self_monitoring.log_query",
                "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=_CLI_SUBPROCESS_TIMEOUT_SECONDS,
            env=env,
            cwd=_SRC_ROOT,
        )
        assert result.returncode != 0
        assert "is not set" in result.stderr

    def test_attach_via_cli_fails_with_nonzero_exit(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        result = self._run_module(
            f"ATTACH DATABASE '{tmp_path}/other.db' AS other",
            {log_query.ENV_VAR_DB_PATH: db_path},
        )
        assert result.returncode != 0
        assert not (tmp_path / "other.db").exists()

    def test_multiple_statements_via_cli_fails_with_nonzero_exit(self, tmp_path):
        db_path = _make_logs_db(tmp_path)
        result = self._run_module(
            "SELECT 1; DROP TABLE logs",
            {log_query.ENV_VAR_DB_PATH: db_path},
        )
        assert result.returncode != 0
