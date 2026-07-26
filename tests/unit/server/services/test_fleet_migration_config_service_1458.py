"""Fleet migration scheduler config (Story #1458, Background Jobs Checklist).

Mirrors test_hnsw_orphan_repair_sweep_config_service_1368.py's proven
pattern, closing the same class of bug (Bug #1368) BEFORE it can recur:
ConfigService._merge_runtime_config() round-trips the full ServerConfig
through dataclasses.asdict() (flattening EVERY nested dataclass field,
including fleet_migration_config, to a plain dict) and
ServerConfigManager._dict_to_server_config() must have an explicit
dict -> dataclass conversion block for it, or a real server restart /
cross-node reload leaves fleet_migration_config as a raw dict and any
`cfg.enabled` attribute access raises AttributeError.

Real ConfigService driven through its actual seed-to-DB-then-reload-from-DB
lifecycle (SQLite solo mode). The third test constructs a FleetMigrationConfig
ONLY to simulate a Web-UI save action's input value -- it is immediately
persisted via the real save_config() -> SQLite write, and the assertion reads
it back via a SEPARATE, freshly-restarted ConfigService instance (a genuine
DB round trip), the SAME pattern the HNSW file's own analogous test uses.
This is distinct from hand-constructing a config and reading it back
in-process with no DB round trip at all (see project memory
feedback_faithful_db_mocks.md for why THAT would be an unfaithful gap).

Safety: fleet migration touches real chunk data (consolidates + deletes old
sharded files) -- unlike the HNSW orphan sweep, this MUST default to
`enabled=False` so a fresh deployment never migrates repos without an
explicit operator opt-in via the Web UI Config Screen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict


def _make_sqlite_runtime_table(db_path: str) -> None:
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


class TestConfigServiceSqliteRoundTrip:
    def test_fresh_server_first_boot_seed_produces_typed_config_disabled_by_default(
        self, tmp_path
    ):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)
        service.initialize_runtime_db(db_path)

        cfg = service.get_config().fleet_migration_config
        # Safety-critical default: fleet migration MUST ship OFF, unlike
        # the HNSW sweep, because it deletes real on-disk chunk data.
        assert bool(cfg.enabled) is False
        assert int(cfg.tick_interval_minutes) == 30

    def test_server_restart_reload_from_sqlite_produces_typed_config(self, tmp_path):
        """The Bug #1368 failure mode reproduced for this new config
        section: before the dict->dataclass conversion block exists,
        `cfg.enabled` raises AttributeError after a restart reload."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import FleetMigrationConfig

        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)

        boot_service = ConfigService(server_dir_path=str(tmp_path))
        boot_service.initialize_runtime_db(db_path)

        restarted_service = ConfigService(server_dir_path=str(tmp_path))
        restarted_service.initialize_runtime_db(db_path)

        cfg = restarted_service.get_config().fleet_migration_config

        assert isinstance(cfg, FleetMigrationConfig), (
            f"Expected FleetMigrationConfig, got {type(cfg).__name__} "
            f"({cfg!r}) -- Bug #1368-class regression: "
            "'dict' object has no attribute 'enabled'"
        )
        assert bool(cfg.enabled) is False
        assert int(cfg.tick_interval_minutes) == 30

    def test_custom_web_ui_values_survive_restart_reload(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import FleetMigrationConfig

        db_path = str(tmp_path / "runtime.db")
        _make_sqlite_runtime_table(db_path)

        boot_service = ConfigService(server_dir_path=str(tmp_path))
        boot_service.initialize_runtime_db(db_path)

        # Simulate an operator explicitly opting in via the Web UI: the
        # constructed value is a stand-in for the Web-UI form submission,
        # immediately persisted via the REAL save_config() -> SQLite write
        # below -- never read back in this same process/instance.
        config = boot_service.get_config()
        config.fleet_migration_config = FleetMigrationConfig(
            enabled=True, tick_interval_minutes=5
        )
        boot_service.save_config(config)

        # A genuinely SEPARATE, freshly-restarted ConfigService instance
        # reloads the persisted value from SQLite -- the real DB round trip
        # this regression guard exists to prove.
        restarted_service = ConfigService(server_dir_path=str(tmp_path))
        restarted_service.initialize_runtime_db(db_path)
        cfg = restarted_service.get_config().fleet_migration_config

        assert isinstance(cfg, FleetMigrationConfig)
        assert bool(cfg.enabled) is True
        assert int(cfg.tick_interval_minutes) == 5
        assert asdict(cfg) == {"enabled": True, "tick_interval_minutes": 5}
