"""Unit tests for migration 052_tool_group_access.sql (Story #1593, AC1).

Story #1593 introduces per-tool, per-group MCP access control. AC1 requires
a new `tool_group_access` table on the PostgreSQL (cluster) backend,
mirroring the SQLite schema added to GroupAccessManager._ensure_schema().

This file verifies the migration FILE's content only, following the
established pattern in test_migration_049_backend_indexes_1697.py, but
with case-insensitive / whitespace-tolerant / comment-stripped parsing
throughout so the tests validate the DECLARED schema shape rather than
one exact rendering of it:

- The migration is numbered one past the true current maximum at the time
  it was created (052, immediately following 051 -- re-verified live via
  `ls .../migrations/sql/ | sort -t_ -k1 -n | tail -5` at implementation
  time, NOT trusted from any number written in the story/epic text).
- It creates the tool_group_access table with the required columns
  (group_id, tool_name, allowed BOOLEAN NOT NULL, granted_by, granted_at).
  Column/FK/uniqueness checks are scoped to the tool_group_access table's
  own balanced-parenthesis CREATE TABLE block, so a definition living on
  some OTHER table in the same file cannot satisfy these assertions.
- group_id SPECIFICALLY has a foreign key to groups(id) with ON DELETE
  CASCADE (PostgreSQL relies on cascade; SQLite got an explicit DELETE
  instead, since SQLite has no cascade wired in GroupAccessManager) --
  checked either as an inline constraint on the group_id column itself or
  as a table-level `FOREIGN KEY (group_id) REFERENCES ...`, so a cascade
  FK on some OTHER column cannot satisfy the assertion.
- It is indexed on group_id and on tool_name via real, parsed CREATE
  INDEX statements targeting tool_group_access (exact column-identifier
  matching, so a column like `other_group_id` cannot satisfy a `group_id`
  index requirement), with a unique constraint on (group_id, tool_name).
- It uses only CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS --
  no DROP of any kind, no RENAME, no ALTER COLUMN -- per this project's
  backward-compatible rolling-restart migration rule.

TDD: written FIRST, before the migration file exists. The two
existence/ordering tests in TestMigration052Exists fail via plain
assertions (the file/number is absent); every other test in this module
reads the migration file's content and so fails with FileNotFoundError
until 052_tool_group_access.sql is created.
"""

import re
from pathlib import Path
from typing import List, Tuple


def _migrations_sql_dir() -> Path:
    import code_indexer.server.storage.postgres.migrations as migrations_pkg

    return Path(migrations_pkg.__file__).parent / "sql"


def _read_migration_052() -> str:
    return (_migrations_sql_dir() / "052_tool_group_access.sql").read_text()


def _strip_sql_comments(content: str) -> str:
    """Strip both block comments (/* ... */) and line comments (-- ...),
    including an inline `-- trailing comment` after real SQL on the same
    line, so downstream parsing/regexes only ever see real DDL."""
    no_block = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", "", no_block)
    return no_line


def _table_definition_block(content: str, table: str) -> str:
    """Extract the balanced-parenthesis column list of
    `CREATE TABLE IF NOT EXISTS <table> ( ... )`, so column/FK/uniqueness
    assertions can be scoped to THIS table and cannot be satisfied by a
    definition that happens to live on some other table in the same file.
    """
    ddl = _strip_sql_comments(content)
    header = re.search(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\(",
        ddl,
        re.IGNORECASE,
    )
    assert header is not None, f"no CREATE TABLE IF NOT EXISTS {table} ( found"

    depth = 1
    start = header.end()
    i = start
    while i < len(ddl) and depth > 0:
        if ddl[i] == "(":
            depth += 1
        elif ddl[i] == ")":
            depth -= 1
        i += 1
    assert depth == 0, f"unbalanced parentheses in {table} definition"
    return ddl[start : i - 1]


def _create_index_statements(content: str) -> List[Tuple[str, str, List[str]]]:
    """Parse real `CREATE [UNIQUE] INDEX IF NOT EXISTS <name> ON <table>(<cols>)`
    statements out of the comment-stripped SQL, case-insensitively, so
    tests validate actual DDL rather than any substring anywhere in the
    file (a comment included).

    Returns a list of (index_name, table_name_lower, [column_lower, ...]).
    """
    ddl = _strip_sql_comments(content)
    pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+"
        r"(?P<name>\w+)\s*\n?\s*ON\s+(?P<table>\w+)\s*\((?P<cols>[^)]*)\)",
        re.IGNORECASE,
    )
    results = []
    for m in pattern.finditer(ddl):
        columns = [c.strip().lower() for c in m.group("cols").split(",")]
        results.append((m.group("name"), m.group("table").lower(), columns))
    return results


def _assert_has_index_on(content: str, column: str) -> None:
    """Assert a real CREATE INDEX IF NOT EXISTS ... ON tool_group_access(...)
    statement exists whose column list contains EXACTLY `column` (not a
    substring match, so `other_group_id` cannot satisfy `group_id`)."""
    statements = _create_index_statements(content)
    matching = [s for s in statements if s[1] == "tool_group_access" and column in s[2]]
    assert matching, (
        f"expected a real CREATE INDEX IF NOT EXISTS ... ON "
        f"tool_group_access(...{column}...) statement, parsed statements: {statements}"
    )


def _column_definition_line(block: str, column: str) -> str:
    """Find the line declaring `column` within a table-definition block
    (as returned by _table_definition_block), case-insensitively, tolerant
    of leading whitespace/indentation."""
    pattern = re.compile(rf"^\s*{re.escape(column)}\b", re.IGNORECASE)
    for line in block.splitlines():
        if pattern.match(line):
            return line
    raise AssertionError(f"no column definition line found for {column!r}")


class TestMigration052Exists:
    def test_file_exists_and_is_named_052(self):
        sql_dir = _migrations_sql_dir()
        assert (sql_dir / "052_tool_group_access.sql").exists()

    def test_is_the_next_migration_after_051(self):
        """052 must exist and immediately follow 051 in the sorted sequence.

        Deliberately NOT asserting 52 is the global max: this test only
        verifies 052's own position relative to 051, so it stays valid
        regardless of how many migrations are added after it.
        """
        sql_dir = _migrations_sql_dir()
        numbers = sorted(
            int(p.name.split("_", 1)[0])
            for p in sql_dir.glob("*.sql")
            if p.name[:3].isdigit()
        )
        assert 52 in numbers
        idx_52 = numbers.index(52)
        assert idx_52 > 0, "052 must have a predecessor in the sorted sequence"
        assert numbers[idx_52 - 1] == 51


class TestMigration052TableAndColumns:
    def test_creates_table_if_not_exists(self):
        # Raises if the table doesn't exist as CREATE TABLE IF NOT EXISTS.
        _table_definition_block(_read_migration_052(), "tool_group_access")

    def test_has_required_columns(self):
        block = _table_definition_block(_read_migration_052(), "tool_group_access")
        for column in ("group_id", "tool_name", "allowed", "granted_by", "granted_at"):
            _column_definition_line(block, column)  # raises if missing

    def test_allowed_is_boolean_not_null(self):
        block = _table_definition_block(_read_migration_052(), "tool_group_access")
        allowed_line = _column_definition_line(block, "allowed").upper()
        assert "BOOLEAN" in allowed_line
        assert "NOT NULL" in allowed_line


class TestMigration052ForeignKeyAndUniqueness:
    def test_group_id_foreign_key_has_on_delete_cascade(self):
        """group_id specifically (not some other column) must reference
        groups(id) with ON DELETE CASCADE -- either as an inline
        column-level constraint on the group_id column definition itself,
        or as a table-level `FOREIGN KEY (group_id) REFERENCES ...`."""
        block = _table_definition_block(
            _read_migration_052(), "tool_group_access"
        ).upper()

        inline_on_group_id_column = re.search(
            r"(?s)\bGROUP_ID\b(?:(?!,).)*?REFERENCES\s+GROUPS\s*\(\s*ID\s*\)"
            r"\s+ON\s+DELETE\s+CASCADE",
            block,
        )
        table_level_fk_on_group_id = re.search(
            r"FOREIGN\s+KEY\s*\(\s*GROUP_ID\s*\)\s+REFERENCES\s+GROUPS\s*\(\s*ID\s*\)"
            r"\s+ON\s+DELETE\s+CASCADE",
            block,
        )
        assert inline_on_group_id_column or table_level_fk_on_group_id, (
            "group_id must have a foreign key to groups(id) ON DELETE "
            f"CASCADE (inline or table-level), got block: {block!r}"
        )

    def test_unique_constraint_on_group_id_and_tool_name(self):
        block = _table_definition_block(
            _read_migration_052(), "tool_group_access"
        ).upper()
        assert re.search(
            r"(UNIQUE|PRIMARY\s+KEY)\s*\(\s*GROUP_ID\s*,\s*TOOL_NAME\s*\)", block
        ), "must enforce uniqueness of (group_id, tool_name)"


class TestMigration052Indexes:
    def test_group_id_has_a_real_index_statement(self):
        _assert_has_index_on(_read_migration_052(), "group_id")

    def test_tool_name_has_a_real_index_statement(self):
        _assert_has_index_on(_read_migration_052(), "tool_name")


class TestMigration052NoDestructiveStatements:
    """Backward-compatible rolling-upgrade safety (CLAUDE.md): additive only."""

    def test_no_drop_statements_of_any_kind(self):
        ddl_only = _strip_sql_comments(_read_migration_052()).upper()
        assert not re.search(r"\bDROP\s+\w+", ddl_only), (
            "migration must not contain any DROP statement"
        )

    def test_no_rename_or_alter_column_statements(self):
        ddl_only = _strip_sql_comments(_read_migration_052()).upper()
        assert "RENAME" not in ddl_only
        assert "ALTER COLUMN" not in ddl_only


class TestMigration052IdempotentCreation:
    def test_every_create_table_uses_if_not_exists(self):
        ddl = _strip_sql_comments(_read_migration_052()).upper()
        total = len(re.findall(r"CREATE\s+TABLE\b", ddl))
        guarded = len(re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b", ddl))
        assert total > 0, "expected at least one CREATE TABLE statement"
        assert total == guarded, (
            "every CREATE TABLE must use IF NOT EXISTS for rolling-restart safety"
        )

    def test_every_create_index_uses_if_not_exists(self):
        ddl = _strip_sql_comments(_read_migration_052()).upper()
        total = len(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\b", ddl))
        guarded = len(
            re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\b", ddl)
        )
        assert total > 0, "expected at least one CREATE INDEX statement"
        assert total == guarded, (
            "every CREATE INDEX must use IF NOT EXISTS for rolling-restart safety"
        )
