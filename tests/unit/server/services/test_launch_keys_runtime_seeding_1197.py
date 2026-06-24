"""Story #1197 AC2 + AC4 + AC6: Runtime seeding, get_config, and save_config.

RED-phase tests — all must FAIL before production code is written.

AC2: initialize_runtime_db seeds host/port/workers/log_level idempotently.
AC4: get_config() surfaces the four keys from the runtime row.
AC6: save_config() retains the four keys in config.json on BOTH paths (MAJOR-5).
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_sqlite_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_config ("
            "config_key TEXT PRIMARY KEY DEFAULT 'runtime', "
            "config_json TEXT NOT NULL, "
            "version INTEGER NOT NULL DEFAULT 1, "
            "updated_at TEXT DEFAULT (datetime('now')), "
            "updated_by TEXT)"
        )
        conn.commit()


def _read_runtime_row(db_path: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT config_json, version FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
    assert row is not None, f"No runtime row in {db_path}"
    return {"data": json.loads(row[0]), "version": row[1]}


def _make_mock_pg_pool(version: int = 1) -> MagicMock:
    """Minimal PG pool mock that satisfies _save_runtime_to_pg and _seed_runtime_to_pg."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = {"version": version}
    mock_conn.execute.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_pool.connection.return_value = mock_conn
    return mock_pool


@pytest.mark.slow
class TestAC2InitializeRuntimeDbSeeds:
    """Story #1197 AC2: initialize_runtime_db seeds the four launch keys."""

    def test_seeds_four_launch_keys_on_first_boot(self, tmp_path: Path) -> None:
        """First boot: empty SQLite — seeds workers/log_level/host/port from config."""
        from code_indexer.server.services.config_service import ConfigService

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        # Inject custom values into the in-memory config before seeding
        config = svc.get_config()
        config.workers = 4
        config.log_level = "DEBUG"
        config.host = "0.0.0.0"
        config.port = 8000
        svc._config = config

        svc.initialize_runtime_db(db_path)

        result = _read_runtime_row(db_path)
        runtime = result["data"]
        assert "workers" in runtime, "AC2: workers must be in runtime row"
        assert "log_level" in runtime, "AC2: log_level must be in runtime row"
        assert "host" in runtime, "AC2: host must be in runtime row"
        assert "port" in runtime, "AC2: port must be in runtime row"
        assert runtime["workers"] == 4
        assert runtime["log_level"] == "DEBUG"
        assert runtime["host"] == "0.0.0.0"
        assert runtime["port"] == 8000

    def test_idempotent_second_run_no_extra_version_churn(self, tmp_path: Path) -> None:
        """Second run must not re-seed and increment version for the four keys."""
        from code_indexer.server.services.config_service import ConfigService

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc1 = ConfigService(server_dir_path=str(tmp_path))
        svc1.load_config()
        svc1.initialize_runtime_db(db_path)
        first_version = _read_runtime_row(db_path)["version"]

        svc2 = ConfigService(server_dir_path=str(tmp_path))
        svc2.load_config()
        svc2.initialize_runtime_db(db_path)
        second_version = _read_runtime_row(db_path)["version"]

        # Allow at most 1 increment (lifecycle_analysis_config auto-seed may fire once)
        assert second_version <= first_version + 1, (
            f"AC2 idempotency: version increased from {first_version} to {second_version} "
            "on second boot — four launch keys must not cause extra version churn"
        )


@pytest.mark.slow
class TestAC4GetConfigSurfacesRuntimeValues:
    """Story #1197 AC4: get_config() returns the four keys from the runtime row."""

    def test_get_config_returns_workers_from_runtime_row(self, tmp_path: Path) -> None:
        from code_indexer.server.services.config_service import ConfigService

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO server_config (config_key, config_json, version, updated_by) "
                "VALUES ('runtime', ?, 1, 'test')",
                (
                    json.dumps(
                        {
                            "workers": 4,
                            "log_level": "DEBUG",
                            "host": "0.0.0.0",
                            "port": 9999,
                        }
                    ),
                ),
            )
            conn.commit()

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc.initialize_runtime_db(db_path)

        config = svc.get_config()
        assert config.workers == 4, (
            f"AC4: expected workers=4 from runtime, got {config.workers}"
        )
        assert config.log_level == "DEBUG", (
            f"AC4: expected log_level='DEBUG', got {config.log_level}"
        )
        assert config.host == "0.0.0.0", (
            f"AC4: expected host='0.0.0.0', got {config.host}"
        )
        assert config.port == 9999, f"AC4: expected port=9999, got {config.port}"


@pytest.mark.slow
class TestAC6SaveConfigRetainsFourKeys:
    """Story #1197 AC6 (MAJOR-5): save_config() keeps the four keys in config.json."""

    def test_sqlite_path_retains_four_launch_keys(self, tmp_path: Path) -> None:
        """After save_config on the SQLite path, config.json still has the four keys."""
        from code_indexer.server.services.config_service import ConfigService

        db_path = str(tmp_path / "cidx_server.db")
        _make_sqlite_db(db_path)

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc.initialize_runtime_db(db_path)

        config = svc.get_config()
        config.workers = 5
        config.log_level = "WARNING"
        config.host = "0.0.0.0"
        config.port = 8001
        svc.save_config(config)

        saved = json.loads((tmp_path / "config.json").read_text())
        assert "workers" in saved, "AC6 (SQLite): workers must survive save_config()"
        assert "log_level" in saved, (
            "AC6 (SQLite): log_level must survive save_config()"
        )
        assert "host" in saved, "AC6 (SQLite): host must survive save_config()"
        assert "port" in saved, "AC6 (SQLite): port must survive save_config()"
        assert saved["workers"] == 5
        assert saved["log_level"] == "WARNING"
        assert saved["host"] == "0.0.0.0"
        assert saved["port"] == 8001

    def test_pg_path_retains_four_launch_keys(self, tmp_path: Path) -> None:
        """After save_config on the PG path, config.json still has the four keys."""
        from code_indexer.server.services.config_service import ConfigService

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        svc._pool = _make_mock_pg_pool(version=1)
        svc._sqlite_db_path = None

        config = svc.get_config()
        config.workers = 6
        config.log_level = "ERROR"
        config.host = "10.0.0.1"
        config.port = 9001
        svc.save_config(config)

        saved = json.loads((tmp_path / "config.json").read_text())
        assert "workers" in saved, "AC6 (PG): workers must survive save_config()"
        assert "log_level" in saved, "AC6 (PG): log_level must survive save_config()"
        assert "host" in saved, "AC6 (PG): host must survive save_config()"
        assert "port" in saved, "AC6 (PG): port must survive save_config()"
        assert saved["workers"] == 6
        assert saved["log_level"] == "ERROR"
        assert saved["host"] == "10.0.0.1"
        assert saved["port"] == 9001
