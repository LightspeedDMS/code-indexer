"""
Persistence tests for next_refresh column in global_repos SQLite backend (Story #284 AC4).

Tests:
- SQLite round-trip: write next_refresh, read back, verify value
- NULL handling: new repos without next_refresh return None
- update_next_refresh(): updates value, returns True on success
- update_next_refresh() with None clears value
- Schema migration: existing database without next_refresh column gets it added
- GlobalRegistry.update_next_refresh() delegates to SQLite backend
- GlobalRegistry JSON backend round-trip
"""

import sqlite3
import time
from pathlib import Path

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.alias_manager import AliasManager


def _init_db(tmp_path: Path) -> tuple[str, GlobalReposSqliteBackend]:
    """Initialize a test database and return (db_path_str, backend)."""
    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    backend = GlobalReposSqliteBackend(str(db_path))
    return str(db_path), backend


def _register_one_repo(backend: GlobalReposSqliteBackend, alias: str = "test-repo-global") -> None:
    """Register a single repo in the backend."""
    backend.register_repo(
        alias_name=alias,
        repo_name=alias.replace("-global", ""),
        repo_url="https://github.com/test/repo.git",
        index_path="/path/to/index",
        enable_temporal=False,
        temporal_options=None,
    )


class TestSqliteNextRefreshRoundTrip:
    """SQLite CRUD tests for next_refresh column."""

    def test_new_repo_has_null_next_refresh(self, tmp_path):
        """A freshly registered repo has next_refresh=None."""
        _, backend = _init_db(tmp_path)
        _register_one_repo(backend)

        result = backend.get_repo("test-repo-global")
        assert result is not None
        assert result.get("next_refresh") is None

    def test_update_next_refresh_persists_value(self, tmp_path):
        """update_next_refresh() stores value readable via get_repo()."""
        _, backend = _init_db(tmp_path)
        _register_one_repo(backend)

        future_time = time.time() + 3600
        success = backend.update_next_refresh("test-repo-global", str(future_time))
        assert success is True

        result = backend.get_repo("test-repo-global")
        assert result is not None
        stored = result.get("next_refresh")
        assert stored is not None
        assert abs(float(stored) - future_time) < 0.001

    def test_update_next_refresh_with_none_clears_value(self, tmp_path):
        """update_next_refresh(alias, None) sets next_refresh to NULL."""
        _, backend = _init_db(tmp_path)
        _register_one_repo(backend)

        # Set then clear
        backend.update_next_refresh("test-repo-global", str(time.time() + 3600))
        backend.update_next_refresh("test-repo-global", None)

        result = backend.get_repo("test-repo-global")
        assert result is not None
        assert result.get("next_refresh") is None

    def test_update_next_refresh_returns_false_for_unknown_alias(self, tmp_path):
        """update_next_refresh() returns False when alias does not exist."""
        _, backend = _init_db(tmp_path)
        result = backend.update_next_refresh("nonexistent-global", str(time.time()))
        assert result is False

    def test_list_repos_includes_next_refresh(self, tmp_path):
        """list_repos() returns next_refresh in each repo dict."""
        _, backend = _init_db(tmp_path)
        _register_one_repo(backend)

        future_time = time.time() + 7200
        backend.update_next_refresh("test-repo-global", str(future_time))

        repos = backend.list_repos()
        assert "test-repo-global" in repos
        stored = repos["test-repo-global"].get("next_refresh")
        assert stored is not None
        assert abs(float(stored) - future_time) < 0.001

    def test_list_repos_null_next_refresh_is_none(self, tmp_path):
        """list_repos() returns None for next_refresh when not set."""
        _, backend = _init_db(tmp_path)
        _register_one_repo(backend)

        repos = backend.list_repos()
        assert repos["test-repo-global"].get("next_refresh") is None

    def test_next_refresh_persists_across_backend_instances(self, tmp_path):
        """next_refresh written by one backend instance is readable by another."""
        db_path_str, backend1 = _init_db(tmp_path)
        _register_one_repo(backend1)

        future_time = time.time() + 5400
        backend1.update_next_refresh("test-repo-global", str(future_time))

        # Create fresh backend pointing to same DB
        backend2 = GlobalReposSqliteBackend(db_path_str)
        result = backend2.get_repo("test-repo-global")
        assert result is not None
        stored = result.get("next_refresh")
        assert stored is not None
        assert abs(float(stored) - future_time) < 0.001


class TestSchemaMigration:
    """Schema migration tests: existing DB without next_refresh gets column added."""

    def test_migration_adds_next_refresh_to_existing_db(self, tmp_path):
        """
        A database created without next_refresh column gets it added via migration.
        """
        db_path = tmp_path / "migrate.db"

        # Create a raw database without next_refresh (simulating old schema)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_repos (
                alias_name TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL,
                repo_url TEXT,
                index_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_refresh TEXT NOT NULL,
                enable_temporal BOOLEAN DEFAULT FALSE,
                temporal_options TEXT,
                enable_scip BOOLEAN DEFAULT FALSE
            )
        """)
        conn.execute(
            "INSERT INTO global_repos VALUES (?,?,?,?,?,?,?,?,?)",
            ("old-repo-global", "old-repo", "https://github.com/t/r.git",
             "/idx", "2025-01-01T00:00:00", "2025-01-01T00:00:00", 0, None, 0),
        )
        conn.commit()
        conn.close()

        # Running initialize_database() should run migration
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        # Verify next_refresh column exists and old data readable
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(global_repos)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "next_refresh" in columns

    def test_migration_existing_rows_get_null_next_refresh(self, tmp_path):
        """After migration, existing rows have NULL next_refresh."""
        db_path = tmp_path / "migrate2.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_repos (
                alias_name TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL,
                repo_url TEXT,
                index_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_refresh TEXT NOT NULL,
                enable_temporal BOOLEAN DEFAULT FALSE,
                temporal_options TEXT,
                enable_scip BOOLEAN DEFAULT FALSE
            )
        """)
        conn.execute(
            "INSERT INTO global_repos VALUES (?,?,?,?,?,?,?,?,?)",
            ("migrated-repo-global", "migrated-repo", "https://github.com/t/r.git",
             "/idx", "2025-01-01T00:00:00", "2025-01-01T00:00:00", 0, None, 0),
        )
        conn.commit()
        conn.close()

        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        backend = GlobalReposSqliteBackend(str(db_path))
        result = backend.get_repo("migrated-repo-global")
        assert result is not None
        assert result.get("next_refresh") is None


class TestGlobalRegistryNextRefresh:
    """Tests for GlobalRegistry.update_next_refresh() with JSON backend."""

    def _make_registry(self, tmp_path: Path) -> tuple[GlobalRegistry, Path]:
        golden_repos_dir = tmp_path / "golden_repos"
        golden_repos_dir.mkdir(parents=True)
        registry = GlobalRegistry(str(golden_repos_dir))
        return registry, golden_repos_dir

    def _register(
        self, registry: GlobalRegistry, golden_repos_dir: Path, alias: str
    ) -> None:
        """Register a git repo in alias + registry."""
        global_alias = f"{alias}-global"
        master_path = golden_repos_dir / alias
        master_path.mkdir(parents=True, exist_ok=True)
        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        alias_mgr.create_alias(global_alias, str(master_path))
        registry.register_global_repo(
            alias,
            global_alias,
            "https://github.com/test/repo.git",
            str(master_path),
        )

    def test_json_backend_update_next_refresh_round_trip(self, tmp_path):
        """
        GlobalRegistry (JSON backend) update_next_refresh() writes and reads back.
        """
        registry, golden_repos_dir = self._make_registry(tmp_path)
        self._register(registry, golden_repos_dir, "json-repo")

        future_time = time.time() + 3600
        registry.update_next_refresh("json-repo-global", future_time)

        repos = registry.list_global_repos()
        assert len(repos) == 1
        stored = repos[0].get("next_refresh")
        assert stored is not None
        assert abs(float(stored) - future_time) < 0.001

    def test_json_backend_new_repo_has_null_next_refresh(self, tmp_path):
        """New repo in JSON backend has no next_refresh."""
        registry, golden_repos_dir = self._make_registry(tmp_path)
        self._register(registry, golden_repos_dir, "fresh-repo")

        repos = registry.list_global_repos()
        assert repos[0].get("next_refresh") is None

    def test_json_backend_update_none_clears_value(self, tmp_path):
        """update_next_refresh(alias, None) clears next_refresh in JSON backend."""
        registry, golden_repos_dir = self._make_registry(tmp_path)
        self._register(registry, golden_repos_dir, "clear-repo")

        registry.update_next_refresh("clear-repo-global", time.time() + 3600)
        registry.update_next_refresh("clear-repo-global", None)

        repos = registry.list_global_repos()
        assert repos[0].get("next_refresh") is None

    def test_json_backend_persists_across_reload(self, tmp_path):
        """next_refresh persists after creating a new GlobalRegistry from same dir."""
        registry1, golden_repos_dir = self._make_registry(tmp_path)
        self._register(registry1, golden_repos_dir, "persist-repo")

        future_time = time.time() + 7200
        registry1.update_next_refresh("persist-repo-global", future_time)

        # Fresh registry instance, same directory
        registry2 = GlobalRegistry(str(golden_repos_dir))
        repos = registry2.list_global_repos()
        assert len(repos) == 1
        stored = repos[0].get("next_refresh")
        assert stored is not None
        assert abs(float(stored) - future_time) < 0.001
