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
        assert len(sql_files) >= 3, (
            f"Expected at least 3 SQL migration files, found {len(sql_files)}"
        )

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
            assert prefix.isdigit(), (
                f"Migration file {f.name} must start with numeric prefix"
            )

    def test_migration_prefixes_are_unique(self):
        """No two migrations may share a numeric prefix (EVO-65021): a collision
        makes their relative order filesystem-dependent and ambiguous. This
        caught the 038 workspace_id/embedding-call-stats collision.

        ``024`` is a KNOWN pre-existing collision on master
        (024_wiki_article_views / 024_token_bucket_state) that predates this work;
        renumbering an already-applied migration is unsafe (schema_migrations
        dedups on filename), so it is allow-listed here rather than renumbered.
        Any NEW collision still fails.
        """
        known_pre_existing = {"024"}
        migrations_dir = Path("src/code_indexer/server/storage/postgres/migrations/sql")
        seen: dict[str, str] = {}
        for f in sorted(migrations_dir.glob("*.sql")):
            prefix = f.stem.split("_")[0]
            if prefix in known_pre_existing:
                continue
            assert prefix not in seen, (
                f"Duplicate migration prefix {prefix}: {f.name} collides with "
                f"{seen[prefix]}"
            )
            seen[prefix] = f.name

    def test_workspace_id_migration_renumbered_to_039(self):
        """EVO-65021: the golden-repo workspace_id migration lives at 039 (038
        was already taken by embedding_call_stats on master)."""
        migrations_dir = Path("src/code_indexer/server/storage/postgres/migrations/sql")
        assert (migrations_dir / "039_golden_repo_workspace_id.sql").exists()
        assert not (migrations_dir / "038_golden_repo_workspace_id.sql").exists()
        # The pre-existing 038 is untouched.
        assert (migrations_dir / "038_embedding_call_stats.sql").exists()
