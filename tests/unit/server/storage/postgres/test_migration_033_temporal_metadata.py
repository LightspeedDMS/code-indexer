"""Unit tests for migration 033_temporal_metadata.sql (Bug #1313 Step 7).

Verifies the migration file exists, is next in sequence after 032, and
contains the additive-only DDL (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF
NOT EXISTS) matching the schema TemporalMetadataPostgresBackend expects.
"""

from pathlib import Path


def _migrations_sql_dir() -> Path:
    import code_indexer.server.storage.postgres.migrations as migrations_pkg

    return Path(migrations_pkg.__file__).parent / "sql"


class TestMigration033Exists:
    def test_file_exists_and_is_named_033(self):
        sql_dir = _migrations_sql_dir()
        assert (sql_dir / "033_temporal_metadata.sql").exists()

    def test_is_the_next_migration_after_032(self):
        sql_dir = _migrations_sql_dir()
        numbers = sorted(
            int(p.name.split("_", 1)[0])
            for p in sql_dir.glob("*.sql")
            if p.name[:3].isdigit()
        )
        assert numbers[-1] == 33
        assert numbers[-2] == 32


class TestMigration033Content:
    def _read(self) -> str:
        sql_dir = _migrations_sql_dir()
        return (sql_dir / "033_temporal_metadata.sql").read_text()

    def test_creates_table_if_not_exists(self):
        content = self._read()
        assert "CREATE TABLE IF NOT EXISTS temporal_metadata" in content

    def test_table_has_composite_primary_key_collection_key_hash_prefix(self):
        content = self._read()
        assert "PRIMARY KEY (collection_key, hash_prefix)" in content

    def test_no_drop_or_rename_statements(self):
        """Backward-compatible rolling-upgrade safety (CLAUDE.md): additive only.

        Strips SQL comment lines (`--`) first so explanatory prose (which may
        mention "DROP"/"RENAME" while describing the additive-only policy)
        doesn't produce a false positive -- only actual DDL statements count.
        """
        ddl_only = "\n".join(
            line
            for line in self._read().splitlines()
            if not line.strip().startswith("--")
        ).upper()
        assert "DROP TABLE" not in ddl_only
        assert "DROP COLUMN" not in ddl_only
        assert "RENAME" not in ddl_only
        assert "ALTER COLUMN" not in ddl_only

    def test_creates_unique_index_on_point_id(self):
        content = self._read()
        assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_temporal_meta_pointid" in content
        assert "ON temporal_metadata (collection_key, point_id)" in content

    def test_creates_index_on_commit_hash(self):
        content = self._read()
        assert "CREATE INDEX IF NOT EXISTS idx_temporal_meta_commit" in content
        assert "ON temporal_metadata (collection_key, commit_hash)" in content

    def test_all_index_creation_uses_if_not_exists(self):
        content = self._read()
        for line in content.splitlines():
            stripped = line.strip().upper()
            if stripped.startswith("CREATE INDEX") or stripped.startswith(
                "CREATE UNIQUE INDEX"
            ):
                assert "IF NOT EXISTS" in stripped, (
                    f"Index creation must be idempotent: {line}"
                )
