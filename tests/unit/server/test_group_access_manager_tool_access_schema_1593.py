"""
Unit tests for the tool_group_access SQLite schema (Story #1593, AC1).

Story #1593 introduces per-tool, per-group access control. AC1 requires a
new `tool_group_access` table on both storage backends. This file covers
ONLY the schema shape on the SQLite (solo) backend:

- Table exists after GroupAccessManager initialization.
- Explicit `allowed BOOLEAN NOT NULL` column (Decision 9: row presence
  alone cannot distinguish "never seeded" from "explicitly revoked").
- `granted_by` / `granted_at` columns mirroring repo_group_access.
- Indexed on group_id and on tool_name.
- Unique constraint on (group_id, tool_name).
- Schema creation is idempotent (repeat _ensure_schema() calls / repeat
  GroupAccessManager construction on the same db file) -- and the table
  still has its correct shape afterward, not merely "no crash".
- delete_group() explicitly deletes the group's tool_group_access rows
  (SQLite has no ON DELETE CASCADE wired here, unlike PostgreSQL).

AC2's manager/backend/Protocol methods (set_tool_access, is_tool_allowed,
etc.) are explicitly OUT of scope for this file -- they land in a later
pair-programming step. These tests therefore talk to the raw sqlite3
connection, not to any tool-access API, since that API does not exist yet.

TDD: written FIRST, against unmodified production code where
tool_group_access does not exist at all -- every test below is expected
to fail (table not found / no such table / no such column) until the
schema change lands.

All PRAGMA helpers below operate against the single fixed, hardcoded
table name "tool_group_access" -- never a caller-supplied string -- so
there is no identifier-interpolation input to validate.
"""

import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Set

import pytest

from code_indexer.server.services.group_access_manager import GroupAccessManager

_TABLE = "tool_group_access"


@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _init_manager(db_path: Path) -> GroupAccessManager:
    return GroupAccessManager(db_path)


def _table_row_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (_TABLE,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _table_columns(db_path: Path) -> Dict[str, sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["name"]: row for row in conn.execute(f"PRAGMA table_info({_TABLE})")
        }
    finally:
        conn.close()


def _foreign_keys(db_path: Path) -> List[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(f"PRAGMA foreign_key_list({_TABLE})"))
    finally:
        conn.close()


def _indexed_columns(db_path: Path) -> Set[str]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        indexes = conn.execute(f"PRAGMA index_list({_TABLE})").fetchall()
        columns: Set[str] = set()
        for idx in indexes:
            # idx["name"] comes from sqlite's own PRAGMA index_list output,
            # not from any external/caller input.
            cols = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
            columns.update(c["name"] for c in cols)
        return columns
    finally:
        conn.close()


class TestToolGroupAccessTableShape:
    """Table existence and its core column set."""

    def test_table_exists_after_init(self, temp_db_path):
        _init_manager(temp_db_path)
        assert _table_row_count(temp_db_path) == 1, (
            "tool_group_access table must exist after GroupAccessManager init"
        )

    def test_allowed_column_is_boolean_not_null(self, temp_db_path):
        _init_manager(temp_db_path)
        columns = _table_columns(temp_db_path)

        assert "allowed" in columns, "tool_group_access must have an allowed column"
        allowed_col = columns["allowed"]
        # Decision 9: allowed must be an explicit NOT NULL boolean column so
        # "never seeded" (no row) and "explicitly revoked" (allowed=0) are
        # distinguishable states -- not a presence-only design.
        assert allowed_col["notnull"] == 1, "allowed column must be NOT NULL"
        assert allowed_col["type"].upper() == "BOOLEAN", (
            f"allowed column must be declared BOOLEAN, got {allowed_col['type']!r}"
        )

    def test_has_all_expected_columns(self, temp_db_path):
        _init_manager(temp_db_path)
        columns = set(_table_columns(temp_db_path).keys())
        assert {"group_id", "tool_name", "granted_by", "granted_at"}.issubset(
            columns
        ), f"Missing columns, have: {columns}"


class TestToolGroupAccessForeignKey:
    def test_group_id_foreign_key_references_groups_id(self, temp_db_path):
        _init_manager(temp_db_path)
        fks = _foreign_keys(temp_db_path)

        matching = [
            fk
            for fk in fks
            if fk["table"] == "groups" and fk["from"] == "group_id" and fk["to"] == "id"
        ]
        assert matching, (
            "tool_group_access.group_id must have a foreign key to groups(id), "
            f"got foreign keys: {[dict(fk) for fk in fks]}"
        )


class TestToolGroupAccessIndexesAndUniqueness:
    def test_indexed_on_group_id(self, temp_db_path):
        _init_manager(temp_db_path)
        assert "group_id" in _indexed_columns(temp_db_path), (
            "tool_group_access must be indexed on group_id"
        )

    def test_indexed_on_tool_name(self, temp_db_path):
        _init_manager(temp_db_path)
        assert "tool_name" in _indexed_columns(temp_db_path), (
            "tool_group_access must be indexed on tool_name"
        )

    def test_unique_constraint_prevents_duplicate_group_tool_rows(self, temp_db_path):
        _init_manager(temp_db_path)

        conn = sqlite3.connect(str(temp_db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            row = conn.execute("SELECT id FROM groups WHERE name = 'admins'").fetchone()
            group_id = row["id"]

            conn.execute(
                "INSERT INTO tool_group_access "
                "(group_id, tool_name, allowed, granted_by, granted_at) "
                "VALUES (?, 'git_push', 1, 'admin', '2026-01-01T00:00:00+00:00')",
                (group_id,),
            )
            conn.commit()

            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tool_group_access "
                    "(group_id, tool_name, allowed, granted_by, granted_at) "
                    "VALUES (?, 'git_push', 0, 'admin', '2026-01-02T00:00:00+00:00')",
                    (group_id,),
                )
                conn.commit()
        finally:
            conn.close()


class TestToolGroupAccessSchemaIdempotency:
    def test_schema_creation_idempotent_across_repeated_init(self, temp_db_path):
        """Repeated GroupAccessManager construction against the same db file
        must not raise, must not duplicate the table, and the table must
        still have its full expected shape afterward."""
        _init_manager(temp_db_path)
        _init_manager(temp_db_path)
        _init_manager(temp_db_path)

        assert _table_row_count(temp_db_path) == 1
        columns = set(_table_columns(temp_db_path).keys())
        assert {
            "group_id",
            "tool_name",
            "allowed",
            "granted_by",
            "granted_at",
        }.issubset(columns)

    def test_ensure_schema_directly_repeated_is_idempotent(self, temp_db_path):
        manager = _init_manager(temp_db_path)
        # Calling the internal schema method again must not raise, and the
        # table must still exist with its expected shape afterward -- a
        # bare "does not raise" check would pass trivially even without
        # this story's schema change, since _ensure_schema() already
        # exists and is already safe to call repeatedly for the OTHER
        # tables it creates.
        manager._ensure_schema()
        manager._ensure_schema()

        assert _table_row_count(temp_db_path) == 1
        columns = set(_table_columns(temp_db_path).keys())
        assert {
            "group_id",
            "tool_name",
            "allowed",
            "granted_by",
            "granted_at",
        }.issubset(columns)


class TestDeleteGroupRemovesToolGroupAccessRows:
    """AC1: SQLite has no ON DELETE CASCADE, so delete_group() must
    explicitly delete the group's tool_group_access rows."""

    def test_delete_group_cleans_up_tool_group_access_rows(self, temp_db_path):
        manager = _init_manager(temp_db_path)
        custom_group = manager.create_group(
            "tool_access_test_group", "temp group for AC1 delete test"
        )

        conn = sqlite3.connect(str(temp_db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO tool_group_access "
                "(group_id, tool_name, allowed, granted_by, granted_at) "
                "VALUES (?, 'search_code', 1, 'admin', '2026-01-01T00:00:00+00:00')",
                (custom_group.id,),
            )
            conn.commit()
        finally:
            conn.close()

        deleted = manager.delete_group(custom_group.id)
        assert deleted is True

        conn = sqlite3.connect(str(temp_db_path))
        try:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM tool_group_access WHERE group_id = ?",
                (custom_group.id,),
            ).fetchone()[0]
        finally:
            conn.close()

        assert remaining == 0, (
            "delete_group() must remove the deleted group's tool_group_access rows"
        )
