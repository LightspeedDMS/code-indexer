"""
Tests for PR #1332 review fix: dedicated consumer_rate_limit_state migration.

Verifies the new numbered migration exists, is discovered by MigrationRunner
in the correct order, and creates a table that is genuinely distinct from
the auth login-limiter's token_bucket_state (no shared PK column, no shared
table name) -- the reviewer-flagged landmine this migration closes.
"""

from pathlib import Path

_SQL_DIR = (
    Path(__file__).parents[5]
    / "src"
    / "code_indexer"
    / "server"
    / "storage"
    / "postgres"
    / "migrations"
    / "sql"
)

_MIGRATION_FILE = _SQL_DIR / "034_consumer_rate_limit_state.sql"


def _ddl_only(content: str) -> str:
    """Strip '--' comment lines, leaving only the executable SQL statement.

    The migration's header comment legitimately references token_bucket_state
    and username by name for rationale (why the tables are separate); the
    schema-isolation guarantee this test enforces is about the actual DDL,
    not the prose explaining it.
    """
    return "\n".join(
        line for line in content.splitlines() if not line.strip().startswith("--")
    )


class TestConsumerRateLimitMigrationFile:
    def test_migration_file_exists(self) -> None:
        assert _MIGRATION_FILE.exists(), f"Missing migration file: {_MIGRATION_FILE}"

    def test_migration_creates_dedicated_table_not_token_bucket_state(self) -> None:
        content = _MIGRATION_FILE.read_text(encoding="utf-8")
        ddl = _ddl_only(content)
        assert "CREATE TABLE IF NOT EXISTS consumer_rate_limit_state" in ddl
        assert "token_bucket_state" not in ddl

    def test_migration_uses_if_not_exists_backward_compatible(self) -> None:
        content = _MIGRATION_FILE.read_text(encoding="utf-8")
        assert "IF NOT EXISTS" in content
        assert "DROP TABLE" not in content
        assert "DROP COLUMN" not in content

    def test_migration_key_column_is_not_username(self) -> None:
        """The PK column must NOT be named 'username' -- that name implies
        identity and invites future confusion/collision with the auth table."""
        content = _MIGRATION_FILE.read_text(encoding="utf-8")
        ddl = _ddl_only(content)
        assert "consumer_key" in ddl
        assert "username" not in ddl

    def test_migration_mirrors_token_bucket_state_columns(self) -> None:
        content = _MIGRATION_FILE.read_text(encoding="utf-8")
        for column in ("tokens", "last_refill", "last_access"):
            assert column in content


class TestMigrationDiscoveryOrder:
    def test_runner_discovers_migration_after_033(self) -> None:
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        # Instantiate without connecting: discover_migrations only globs the
        # filesystem, so bypass __init__'s psycopg.connect() by using __new__.
        runner = MigrationRunner.__new__(MigrationRunner)
        runner._sql_dir = _SQL_DIR
        migrations = runner.discover_migrations()
        names = [m.name for m in migrations]
        assert "034_consumer_rate_limit_state.sql" in names
        idx_033 = names.index("033_temporal_metadata.sql")
        idx_034 = names.index("034_consumer_rate_limit_state.sql")
        assert idx_034 > idx_033
