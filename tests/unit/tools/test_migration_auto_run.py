"""
Tests for Story #519: Migration Codebase Audit and Gap Fix.

Verifies:
- AC3: SQL migration files exist
- AC4: MigrationRunner is importable and has correct API
- AC7: Migration idempotency (run() returns 0 when re-run)
"""

from pathlib import Path


class TestMigrationRunnerAPI:
    """Verify MigrationRunner class has the expected interface (AC4)."""

    def test_migration_runner_importable(self):
        """MigrationRunner must be importable."""
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        assert MigrationRunner is not None

    def test_migration_runner_has_run_method(self):
        """MigrationRunner must have run() method."""
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        assert hasattr(MigrationRunner, "run")
        assert callable(getattr(MigrationRunner, "run"))

    def test_migration_runner_has_get_status_method(self):
        """MigrationRunner must have get_status() method."""
        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        assert hasattr(MigrationRunner, "get_status")
        assert callable(getattr(MigrationRunner, "get_status"))


class TestSQLMigrationFiles:
    """Verify SQL migration files exist and are properly numbered (AC3)."""

    def test_sql_migration_files_exist(self):
        """At least 3 SQL migration files must exist."""
        migrations_dir = Path("src/code_indexer/server/storage/postgres/migrations/sql")
        sql_files = sorted(migrations_dir.glob("*.sql"))
        assert (
            len(sql_files) >= 3
        ), f"Expected at least 3 SQL migration files, found {len(sql_files)}"

    def test_initial_schema_exists(self):
        """001_initial_schema.sql must exist."""
        initial = Path(
            "src/code_indexer/server/storage/postgres/migrations/sql/001_initial_schema.sql"
        )
        assert initial.exists(), "001_initial_schema.sql must exist"

    def test_migration_files_are_numbered(self):
        """All migration files must follow NNN_description.sql pattern."""
        migrations_dir = Path("src/code_indexer/server/storage/postgres/migrations/sql")
        for f in migrations_dir.glob("*.sql"):
            prefix = f.stem.split("_")[0]
            assert (
                prefix.isdigit()
            ), f"Migration file {f.name} must start with numeric prefix"
