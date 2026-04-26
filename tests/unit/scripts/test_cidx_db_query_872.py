"""
Unit tests for Story #872: cidx-db-query.sh shell script.

Tests verify:
- Auto-detection of SQLite when no config.json present (AC1)
- Auto-detection of SQLite when config.json has storage_mode=sqlite (AC1)
- Auto-detection of PostgreSQL from config.json postgres_dsn (AC2)
- Scope enforcement rejects --db path outside CIDX data dir (AC4)
- CRUD operations against real SQLite db (AC3)
- Error propagation for invalid SQL (error handling)

Following TDD methodology: Tests written FIRST before implementing (RED phase).
"""

import json
import os
import sqlite3
import stat
import subprocess
import pytest
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[3] / "scripts" / "cidx-db-query.sh"

# Neutral test DSN — no real credentials, obviously placeholder
_TEST_PG_DSN = "postgresql://testuser@localhost/testdb"


def _run_script(args, env=None, extra_env=None):
    """Run cidx-db-query.sh with given args and env overrides."""
    run_env = os.environ.copy()
    if env is not None:
        run_env = env
    if extra_env:
        run_env.update(extra_env)
    return subprocess.run(
        [str(SCRIPT_PATH)] + args,
        capture_output=True,
        text=True,
        env=run_env,
    )


@pytest.fixture
def sqlite_data_dir(tmp_path):
    """
    Create a minimal CIDX data dir with a real SQLite database.
    Structure: tmp_path/data/cidx_server.db
    Uses context manager to guarantee connection release.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "cidx_server.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE server_logs (level TEXT, message TEXT)")
        conn.execute("INSERT INTO server_logs VALUES ('INFO', 'hello')")
        conn.commit()
    return tmp_path


@pytest.mark.parametrize(
    "config_content",
    [
        pytest.param(None, id="no_config_json"),
        pytest.param({"storage_mode": "sqlite"}, id="config_sqlite_mode"),
    ],
)
def test_auto_detect_sqlite(sqlite_data_dir, config_content):
    """
    AC1: When config.json is absent OR has storage_mode=sqlite, script auto-detects
    $CIDX_SERVER_DATA_DIR/data/cidx_server.db and returns results.
    """
    if config_content is not None:
        (sqlite_data_dir / "config.json").write_text(json.dumps(config_content))

    result = _run_script(
        ["SELECT level FROM server_logs LIMIT 1"],
        extra_env={"CIDX_SERVER_DATA_DIR": str(sqlite_data_dir)},
    )
    assert result.returncode == 0, (
        f"Expected exit 0 for SQLite auto-detect. stderr: {result.stderr!r}"
    )
    assert "level" in result.stdout.lower() or "INFO" in result.stdout, (
        f"Expected column header or data in output. Got: {result.stdout!r}"
    )


def test_auto_detect_postgres(tmp_path):
    """
    AC2: When config.json has storage_mode=postgres and postgres_dsn, script
    calls psql with the DSN. A shim captures the invocation.
    """
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    psql_shim = shim_dir / "psql"
    psql_shim.write_text("#!/bin/sh\necho \"SHIM_CALLED: $@\"\n")
    psql_shim.chmod(psql_shim.stat().st_mode | stat.S_IEXEC)

    config = {"storage_mode": "postgres", "postgres_dsn": _TEST_PG_DSN}
    (tmp_path / "config.json").write_text(json.dumps(config))

    env = os.environ.copy()
    env["PATH"] = str(shim_dir) + ":" + env.get("PATH", "")
    env["CIDX_SERVER_DATA_DIR"] = str(tmp_path)

    result = _run_script(["SELECT 1"], env=env)

    assert result.returncode == 0, (
        f"Expected exit 0 when psql shim succeeds. stderr: {result.stderr!r}"
    )
    assert "SHIM_CALLED" in result.stdout, (
        f"Expected psql shim to be called. stdout: {result.stdout!r}"
    )
    assert _TEST_PG_DSN in result.stdout, (
        f"Expected DSN to be passed to psql. stdout: {result.stdout!r}"
    )
    assert "SELECT 1" in result.stdout, (
        f"Expected SQL to be passed to psql. stdout: {result.stdout!r}"
    )


def test_scope_enforcement_rejects_out_of_scope_db(tmp_path):
    """
    AC4: --db pointing outside $CIDX_SERVER_DATA_DIR must exit 1 with
    'target database is outside CIDX data directory' in stderr.

    Uses two separate tmp_path sub-dirs to avoid hardcoding OS-specific paths.
    """
    cidx_data_dir = tmp_path / "cidx_data"
    cidx_data_dir.mkdir()
    (cidx_data_dir / "data").mkdir()

    out_of_scope_dir = tmp_path / "other"
    out_of_scope_dir.mkdir()
    out_of_scope_db = out_of_scope_dir / "other.db"
    with sqlite3.connect(str(out_of_scope_db)) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()

    result = _run_script(
        ["--db", str(out_of_scope_db), "SELECT 1"],
        extra_env={"CIDX_SERVER_DATA_DIR": str(cidx_data_dir)},
    )
    assert result.returncode == 1, (
        f"Expected exit 1 for out-of-scope --db. Got: {result.returncode}. "
        f"stderr: {result.stderr!r}"
    )
    assert "target database is outside CIDX data directory" in result.stderr, (
        f"Expected scope error message in stderr. Got: {result.stderr!r}"
    )


def test_crud_against_real_sqlite(sqlite_data_dir):
    """
    AC3: Full CRUD (CREATE TABLE, INSERT, SELECT) against the real SQLite database.
    """
    result = _run_script(
        [
            "CREATE TABLE IF NOT EXISTS t (x INTEGER);"
            " INSERT INTO t VALUES (42);"
            " SELECT x FROM t"
        ],
        extra_env={"CIDX_SERVER_DATA_DIR": str(sqlite_data_dir)},
    )
    assert result.returncode == 0, (
        f"Expected exit 0 for CRUD operations. stderr: {result.stderr!r}"
    )
    assert "42" in result.stdout, (
        f"Expected '42' in output after INSERT+SELECT. Got: {result.stdout!r}"
    )


def test_error_propagation_invalid_sql(sqlite_data_dir):
    """
    Error propagation: invalid SQL must produce non-zero exit and sqlite3
    error indicator in stderr.
    """
    result = _run_script(
        ["INVALID SQL STATEMENT THAT DOES NOT PARSE"],
        extra_env={"CIDX_SERVER_DATA_DIR": str(sqlite_data_dir)},
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit for invalid SQL. Got returncode={result.returncode}. "
        f"stderr: {result.stderr!r}"
    )
    assert any(
        indicator in result.stderr.lower()
        for indicator in ["syntax error", "near", "error"]
    ), (
        f"Expected sqlite3 error indicator in stderr. Got: {result.stderr!r}"
    )
