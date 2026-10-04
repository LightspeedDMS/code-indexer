"""Migrated SQLite templates for the Bug #2022 refresh harness.

Building a fresh server database runs every DDL statement as its own durable
transaction (~40 fdatasyncs). The harness pays that once per process and
copies the migrated file instead; the copy must be indistinguishable from a
fresh migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Tuple

from code_indexer.server.storage.database_manager import DatabaseSchema
from tests.utils.sqlite_db_templates import copy_migrated_sqlite_db

#: Tables the refresh harness's registry and metadata store depend on.
HARNESS_TABLES = (
    "global_repos",
    "refresh_failure_backoff_state",
    "refresh_trigger_generation_counter",
)


def _schema(db_path: Path) -> List[Tuple[str, str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return [(str(t), str(n), str(s)) for t, n, s in rows]


def _initialize(db_path: Path) -> None:
    DatabaseSchema(str(db_path)).initialize_database()


def test_copy_has_the_schema_of_a_fresh_initialization(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh" / "cidx_server.db"
    _initialize(fresh)

    copied = tmp_path / "copied" / "cidx_server.db"
    copy_migrated_sqlite_db("test-server-db-schema", _initialize, copied)
    _initialize(copied)  # callers re-run the idempotent production init

    copied_schema = _schema(copied)
    assert copied_schema == _schema(fresh)
    tables = {name for kind, name, _ in copied_schema if kind == "table"}
    assert set(HARNESS_TABLES) <= tables


def test_template_is_built_once_per_key(tmp_path: Path) -> None:
    builds: List[Path] = []

    def _build(db_path: Path) -> None:
        builds.append(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.commit()
        finally:
            conn.close()

    first = tmp_path / "a" / "x.db"
    second = tmp_path / "b" / "x.db"
    copy_migrated_sqlite_db("test-built-once", _build, first)
    copy_migrated_sqlite_db("test-built-once", _build, second)

    assert len(builds) == 1
    conn = sqlite3.connect(str(first))
    try:
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(str(second))
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()
