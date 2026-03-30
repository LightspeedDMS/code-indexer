"""
Tests for Story #578: Centralize Runtime Configuration in PostgreSQL.

Tests the bootstrap/runtime config split, PG read/write, merge logic,
version polling, and standalone mode preservation.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.config_service import (
    ConfigService,
    BOOTSTRAP_KEYS,
)


def _make_mock_pool(select_row=None):
    """Create a mock PG connection pool.

    Args:
        select_row: dict to return from fetchone(), or None.
    """
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = select_row

    mock_conn.execute.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_pool.connection.return_value = mock_conn
    return mock_pool, mock_conn


@pytest.fixture
def tmp_server_dir():
    """Create a temporary server directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def config_service(tmp_server_dir):
    """Create a ConfigService with a temporary server directory."""
    svc = ConfigService(server_dir_path=tmp_server_dir)
    svc.load_config()
    return svc


class TestExtractRuntimeDict:
    """Verify _extract_runtime_dict excludes bootstrap keys."""

    def test_extract_runtime_dict_excludes_bootstrap(self, config_service):
        config = config_service.get_config()
        runtime = ConfigService._extract_runtime_dict(config)
        for key in BOOTSTRAP_KEYS:
            assert key not in runtime, f"Bootstrap key '{key}' found in runtime dict"


class TestPoolInitialization:
    """Verify _pool attribute defaults."""

    def test_pool_is_none_by_default(self, config_service):
        assert config_service._pool is None


class TestCheckConfigUpdate:
    """Verify check_config_update behavior."""

    def test_check_config_update_noop_when_no_pool(self, config_service):
        result = config_service.check_config_update()
        assert result is False

    def test_check_config_update_detects_version_change(self, config_service):
        import json as _json

        runtime_dict = {
            "service_display_name": "Reloaded",
            "jwt_expiration_minutes": 42,
        }
        # First call returns version 2, triggering reload
        # _load_runtime_from_pg will also call pool.connection - mock both
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_pool.connection.return_value = mock_conn

        # check_config_update SELECT returns version 2
        # _load_runtime_from_pg SELECT returns full row
        version_cursor = MagicMock()
        version_cursor.fetchone.return_value = {"version": 2}
        full_cursor = MagicMock()
        full_cursor.fetchone.return_value = {
            "config_json": _json.dumps(runtime_dict),
            "version": 2,
        }
        mock_conn.execute.side_effect = [version_cursor, full_cursor]

        config_service._pool = mock_pool
        config_service._db_config_version = 1

        result = config_service.check_config_update()
        assert result is True
        assert config_service._db_config_version == 2
        assert config_service.get_config().service_display_name == "Reloaded"


class TestSaveConfigStandalone:
    """Verify standalone mode saves full config to file."""

    def test_save_config_writes_full_file_in_standalone(
        self, config_service, tmp_server_dir
    ):
        config = config_service.get_config()
        config.service_display_name = "TestNode"
        config_service.save_config(config)

        config_path = os.path.join(tmp_server_dir, "config.json")
        with open(config_path) as f:
            saved = json.load(f)

        # In standalone, full config is written including bootstrap keys
        assert "host" in saved
        assert "port" in saved
        # And runtime keys
        assert saved["service_display_name"] == "TestNode"


class TestSaveConfigCluster:
    """Verify cluster mode saves runtime to PG and bootstrap to file."""

    def test_save_config_writes_to_pg_in_cluster_mode(
        self, config_service, tmp_server_dir
    ):
        # Finding 2 fix: _save_runtime_to_pg no longer uses dict_row,
        # so mock must return a tuple instead of a dict.
        mock_pool, mock_conn = _make_mock_pool(select_row=(2,))
        config_service._pool = mock_pool
        config_service._db_config_version = 1

        config = config_service.get_config()
        config.service_display_name = "ClusterNode"
        config_service.save_config(config)

        # Verify PG was written (UPDATE called)
        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("UPDATE server_config" in c for c in calls)

        # Verify file was also written (bootstrap-only)
        config_path = os.path.join(tmp_server_dir, "config.json")
        with open(config_path) as f:
            saved = json.load(f)
        # Bootstrap keys should be in file
        assert "host" in saved
        assert "port" in saved


class TestSeedRuntimeToPg:
    """Verify first-boot seeding of runtime config to PG."""

    def test_seed_runtime_to_pg_on_empty_table(self, config_service):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_pool.connection.return_value = mock_conn

        # Finding 4 fix: _seed_runtime_to_pg now does INSERT then SELECT.
        # First execute (INSERT) returns a cursor not used for fetchone.
        # Second execute (SELECT version) returns the seeded version.
        insert_cursor = MagicMock()
        select_cursor = MagicMock()
        select_cursor.fetchone.return_value = (1,)
        mock_conn.execute.side_effect = [insert_cursor, select_cursor]

        config_service._pool = mock_pool

        config_service._seed_runtime_to_pg()

        # Verify INSERT was called
        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("INSERT INTO server_config" in c for c in calls)
        assert config_service._db_config_version == 1


class TestLoadRuntimeFromPg:
    """Verify loading and merging runtime config from PG."""

    def test_load_runtime_from_pg_merges_correctly(self, config_service):
        import json as _json

        runtime_dict = {"service_display_name": "FromPG", "jwt_expiration_minutes": 99}
        pg_row = {"config_json": _json.dumps(runtime_dict), "version": 5}

        mock_pool, mock_conn = _make_mock_pool(select_row=pg_row)
        config_service._pool = mock_pool

        original_host = config_service.get_config().host
        config_service._load_runtime_from_pg()

        config = config_service.get_config()
        # Runtime values should be overwritten from PG
        assert config.service_display_name == "FromPG"
        assert config.jwt_expiration_minutes == 99
        # Bootstrap values should be preserved
        assert config.host == original_host
        assert config_service._db_config_version == 5


class TestSetConnectionPool:
    """Verify set_connection_pool wires PG mode."""

    def test_set_connection_pool_loads_from_pg(self, config_service):
        import json as _json

        runtime_dict = {"service_display_name": "Wired"}
        mock_pool, mock_conn = _make_mock_pool(
            select_row={"config_json": _json.dumps(runtime_dict), "version": 3}
        )

        config_service.set_connection_pool(mock_pool)

        assert config_service._pool is mock_pool
        assert config_service._db_config_version == 3
        assert config_service.get_config().service_display_name == "Wired"


class TestSaveConfigDict:
    """Verify save_config_dict writes partial config."""

    def test_save_config_dict_writes_partial_config(
        self, config_service, tmp_server_dir
    ):
        partial = {"host": "0.0.0.0", "port": 9000}
        config_service.config_manager.save_config_dict(partial)

        config_path = os.path.join(tmp_server_dir, "config.json")
        with open(config_path) as f:
            saved = json.load(f)

        assert saved == {"host": "0.0.0.0", "port": 9000}
        # Runtime keys should NOT be in the file
        assert "service_display_name" not in saved


class TestConfigReloadThread:
    """Verify periodic config reload thread lifecycle."""

    def test_start_stop_config_reload_thread(self, config_service):
        import json as _json
        import time

        runtime_dict = {"service_display_name": "Initial"}
        mock_pool, mock_conn = _make_mock_pool(
            select_row={"config_json": _json.dumps(runtime_dict), "version": 1}
        )
        config_service._pool = mock_pool
        config_service._db_config_version = 1

        config_service.start_config_reload(interval_seconds=1)
        assert config_service._reload_thread is not None
        assert config_service._reload_thread.is_alive()

        config_service.stop_config_reload()
        time.sleep(0.2)
        assert not config_service._reload_thread.is_alive()


class TestExtractBootstrapDict:
    """Verify _extract_bootstrap_dict only has bootstrap keys."""

    def test_extract_bootstrap_dict_only_has_bootstrap(self, config_service):
        config = config_service.get_config()
        bootstrap = ConfigService._extract_bootstrap_dict(config)
        for key in bootstrap:
            assert key in BOOTSTRAP_KEYS, (
                f"Non-bootstrap key '{key}' found in bootstrap dict"
            )


class TestInitializeRuntimeDb:
    """Verify initialize_runtime_db migrates config from file to SQLite."""

    def test_initialize_runtime_db_migrates_from_file(self, tmp_server_dir):
        """First boot: config.json has full config, SQLite is empty.

        After initialize_runtime_db:
        - SQLite server_config table has a 'runtime' row
        - config.json is stripped to bootstrap-only keys
        - Backup of original config.json is created
        """
        import sqlite3

        svc = ConfigService(server_dir_path=tmp_server_dir)
        svc.load_config()
        config = svc.get_config()
        original_display_name = config.service_display_name

        # Create SQLite DB with server_config table
        db_path = os.path.join(tmp_server_dir, "data", "cidx_server.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()
        conn.close()

        # Act
        svc.initialize_runtime_db(db_path)

        # Assert: SQLite has runtime row
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT config_json, version FROM server_config "
            "WHERE config_key = 'runtime'"
        ).fetchone()
        conn.close()
        assert row is not None, "Runtime row should exist in SQLite"
        runtime_dict = json.loads(row[0])
        assert "service_display_name" in runtime_dict
        assert runtime_dict["service_display_name"] == original_display_name

        # Assert: config.json stripped to bootstrap only
        config_path = os.path.join(tmp_server_dir, "config.json")
        with open(config_path) as f:
            saved = json.load(f)
        for key in saved:
            assert key in BOOTSTRAP_KEYS, (
                f"Non-bootstrap key '{key}' in config.json after migration"
            )

        # Assert: backup created
        backup_path = os.path.join(
            tmp_server_dir,
            "config-migration-backup",
            "config.json.pre-centralization",
        )
        assert os.path.exists(backup_path)

    def test_initialize_runtime_db_loads_existing(self, tmp_server_dir):
        """Already migrated: SQLite has runtime row.

        After initialize_runtime_db:
        - Runtime values loaded from SQLite and merged into config
        - Version tracked from DB
        """
        import sqlite3

        svc = ConfigService(server_dir_path=tmp_server_dir)
        svc.load_config()

        # Create SQLite DB with existing runtime config
        db_path = os.path.join(tmp_server_dir, "data", "cidx_server.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        runtime = {
            "service_display_name": "FromSQLite",
            "jwt_expiration_minutes": 77,
        }
        conn.execute(
            "INSERT INTO server_config "
            "(config_key, config_json, version, updated_by) "
            "VALUES ('runtime', ?, 5, 'test')",
            (json.dumps(runtime),),
        )
        conn.commit()
        conn.close()

        # Act
        svc.initialize_runtime_db(db_path)

        # Assert: config merged from SQLite
        config = svc.get_config()
        assert config.service_display_name == "FromSQLite"
        assert config.jwt_expiration_minutes == 77
        assert svc._db_config_version == 5


class TestSaveConfigSqlite:
    """Verify save_config writes to SQLite when runtime DB initialized."""

    def test_save_config_writes_to_sqlite_in_solo(self, tmp_server_dir):
        """After initialize_runtime_db, save_config writes runtime to SQLite."""
        import sqlite3

        svc = ConfigService(server_dir_path=tmp_server_dir)
        svc.load_config()

        # Create SQLite DB
        db_path = os.path.join(tmp_server_dir, "data", "cidx_server.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()
        conn.close()

        svc.initialize_runtime_db(db_path)

        # Act: modify config and save
        config = svc.get_config()
        config.service_display_name = "UpdatedViaUI"
        svc.save_config(config)

        # Assert: SQLite updated
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
        conn.close()
        assert row is not None
        runtime = json.loads(row[0])
        assert runtime["service_display_name"] == "UpdatedViaUI"

        # Assert: config.json only has bootstrap keys
        config_path = os.path.join(tmp_server_dir, "config.json")
        with open(config_path) as f:
            saved = json.load(f)
        assert "service_display_name" not in saved

    def test_save_config_writes_to_pg_after_pool_set(self, tmp_server_dir):
        """After set_connection_pool, save_config uses PG over SQLite."""
        import sqlite3

        svc = ConfigService(server_dir_path=tmp_server_dir)
        svc.load_config()

        # Initialize SQLite first
        db_path = os.path.join(tmp_server_dir, "data", "cidx_server.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()
        conn.close()
        svc.initialize_runtime_db(db_path)

        # Set PG pool (cluster mode override)
        runtime_dict = {"service_display_name": "PGNode"}
        mock_pool, mock_conn = _make_mock_pool(
            select_row={
                "config_json": json.dumps(runtime_dict),
                "version": 10,
            }
        )
        svc.set_connection_pool(mock_pool)

        # Re-mock for save (UPDATE + SELECT version)
        update_cursor = MagicMock()
        select_cursor = MagicMock()
        select_cursor.fetchone.return_value = (11,)
        mock_conn.execute.side_effect = [update_cursor, select_cursor]

        config = svc.get_config()
        config.service_display_name = "ClusterSave"
        svc.save_config(config)

        # Verify PG was called
        calls = [str(c) for c in mock_conn.execute.call_args_list]
        assert any("UPDATE server_config" in c for c in calls)


class TestAutoMigrationBackup:
    """Verify backup is created only once during migration."""

    def test_auto_migration_creates_backup_only_once(self, tmp_server_dir):
        """Calling initialize_runtime_db twice should not overwrite backup."""
        import sqlite3

        svc = ConfigService(server_dir_path=tmp_server_dir)
        svc.load_config()

        db_path = os.path.join(tmp_server_dir, "data", "cidx_server.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()
        conn.close()

        # First call: creates backup
        svc.initialize_runtime_db(db_path)
        backup_path = os.path.join(
            tmp_server_dir,
            "config-migration-backup",
            "config.json.pre-centralization",
        )
        assert os.path.exists(backup_path)
        with open(backup_path) as f:
            first_backup = f.read()

        # Modify config.json (simulate drift)
        config_path = os.path.join(tmp_server_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"host": "changed"}, f)

        # Second call: should NOT overwrite backup
        svc2 = ConfigService(server_dir_path=tmp_server_dir)
        svc2.load_config()
        svc2.initialize_runtime_db(db_path)

        with open(backup_path) as f:
            second_backup = f.read()
        assert first_backup == second_backup
