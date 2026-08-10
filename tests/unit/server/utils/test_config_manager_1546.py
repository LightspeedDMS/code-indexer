"""Issue #1546 Phase 2: AliasLockConfig -- the operator-controlled rollout
gate for the DB-backed golden-repo alias lock.

Mirrors test_config_manager_1548.py's TemporalLegacyMigrationConfig
pattern exactly: default OFF (old file-based WriteLockManager mechanism
stays active), Web-UI-configurable, additive/unknown-key-tolerant dict
conversion for rolling-upgrade safety.
"""

from code_indexer.server.utils.config_manager import (
    AliasLockConfig,
    ServerConfig,
    ServerConfigManager,
)


def test_server_config_defaults_alias_lock_config():
    config = ServerConfig(server_dir="/tmp/does-not-matter")
    assert isinstance(config.alias_lock_config, AliasLockConfig)
    assert config.alias_lock_config.db_backed_enabled is False


def test_server_config_converts_persisted_dict_to_dataclass(tmp_path):
    manager = ServerConfigManager(str(tmp_path))
    persisted = {
        "server_dir": str(tmp_path),
        "alias_lock_config": {
            "db_backed_enabled": True,
            "unknown_future_field": "ignored",
        },
    }
    config = manager._dict_to_server_config(persisted)
    assert isinstance(config.alias_lock_config, AliasLockConfig)
    assert config.alias_lock_config.db_backed_enabled is True
