"""Unit tests for migration 049_backend_indexes_1697.sql (Issue #1697).

Issue #1697's systematic sweep deletes several PostgreSQL backends' dead
`_ensure_schema()` self-heal blocks (proven dead in production because
`MigrationRunner` always runs before any backend is constructed). Two of
those dead blocks also created indexes that no migration ever replicated:

- `RefreshTokenPostgresBackend._ensure_schema()` created
  `idx_token_expires ON refresh_tokens (expires_at)` -- absent from
  migration 025_runtime_only_tables.sql.
- `SelfMonitoringPostgresBackend._ensure_schema()` created
  `idx_sm_scans_started_at ON self_monitoring_scans (started_at)` and
  `idx_sm_issues_created_at ON self_monitoring_issues (created_at)` --
  absent from migration 001_initial_schema.sql.

Deleting those `_ensure_schema()` bodies outright (mirroring the Bug
#1655/#1662 precedent) would silently drop index coverage for any BRAND
NEW cluster install created after this fix ships (existing deployments
already have the indexes on disk from a prior boot). This migration
closes that gap the correct way: as a proper, idempotent, additive-only
migration -- the single source of truth going forward -- rather than
leaving the index creation duplicated in backend code that will drift
again.
"""

from pathlib import Path


def _migrations_sql_dir() -> Path:
    import code_indexer.server.storage.postgres.migrations as migrations_pkg

    return Path(migrations_pkg.__file__).parent / "sql"


def _read_migration_049() -> str:
    return (_migrations_sql_dir() / "049_backend_indexes_1697.sql").read_text()


class TestMigration049Exists:
    def test_file_exists_and_is_named_049(self):
        sql_dir = _migrations_sql_dir()
        assert (sql_dir / "049_backend_indexes_1697.sql").exists()

    def test_is_the_next_migration_after_048(self):
        """049 must exist and immediately follow 048 in the sorted sequence.

        Deliberately NOT asserting 49 is the global max: this test only
        verifies 049's own position relative to 048, so it stays valid
        regardless of how many migrations are added after it.
        """
        sql_dir = _migrations_sql_dir()
        numbers = sorted(
            int(p.name.split("_", 1)[0])
            for p in sql_dir.glob("*.sql")
            if p.name[:3].isdigit()
        )
        assert 49 in numbers
        idx_49 = numbers.index(49)
        assert idx_49 > 0, "049 must have a predecessor in the sorted sequence"
        assert numbers[idx_49 - 1] == 48


class TestMigration049CreatesIndexes:
    def test_creates_refresh_token_expires_index(self):
        content = _read_migration_049()
        assert "CREATE INDEX IF NOT EXISTS idx_token_expires" in content
        assert "ON refresh_tokens (expires_at)" in content

    def test_creates_self_monitoring_scans_started_at_index(self):
        content = _read_migration_049()
        assert "CREATE INDEX IF NOT EXISTS idx_sm_scans_started_at" in content
        assert "ON self_monitoring_scans (started_at)" in content

    def test_creates_self_monitoring_issues_created_at_index(self):
        content = _read_migration_049()
        assert "CREATE INDEX IF NOT EXISTS idx_sm_issues_created_at" in content
        assert "ON self_monitoring_issues (created_at)" in content


class TestMigration049Safety:
    def test_no_drop_or_rename_statements(self):
        """Backward-compatible rolling-upgrade safety (CLAUDE.md): additive only."""
        ddl_only = "\n".join(
            line
            for line in _read_migration_049().splitlines()
            if not line.strip().startswith("--")
        ).upper()
        assert "DROP TABLE" not in ddl_only
        assert "DROP COLUMN" not in ddl_only
        assert "RENAME" not in ddl_only
        assert "ALTER COLUMN" not in ddl_only

    def test_all_index_creation_uses_if_not_exists(self):
        content = _read_migration_049()
        for line in content.splitlines():
            stripped = line.strip().upper()
            if stripped.startswith("CREATE INDEX") or stripped.startswith(
                "CREATE UNIQUE INDEX"
            ):
                assert "IF NOT EXISTS" in stripped, (
                    f"Index creation must be idempotent: {line}"
                )
