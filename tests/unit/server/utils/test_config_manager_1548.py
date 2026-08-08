"""Issue #1548: TemporalLegacyMigrationConfig gets its own config section."""

from code_indexer.server.utils.config_manager import (
    FleetMigrationConfig,
    ServerConfig,
    ServerConfigManager,
    TemporalLegacyMigrationConfig,
)


def test_server_config_defaults_temporal_legacy_migration_config():
    config = ServerConfig(server_dir="/tmp/does-not-matter")
    assert isinstance(
        config.temporal_legacy_migration_config, TemporalLegacyMigrationConfig
    )
    assert config.temporal_legacy_migration_config.relocation_enabled is False
    assert config.temporal_legacy_migration_config.cleanup_authorized is False


def test_fleet_migration_config_no_longer_carries_temporal_legacy_fields():
    fm = FleetMigrationConfig()
    assert not hasattr(fm, "temporal_legacy_relocation_enabled")
    assert not hasattr(fm, "temporal_legacy_cleanup_authorized")


def test_server_config_converts_persisted_dict_to_dataclass(tmp_path):
    manager = ServerConfigManager(str(tmp_path))
    persisted = {
        "server_dir": str(tmp_path),
        "temporal_legacy_migration_config": {
            "relocation_enabled": True,
            "cleanup_authorized": True,
            "unknown_future_field": "ignored",
        },
    }
    config = manager._dict_to_server_config(persisted)
    assert isinstance(
        config.temporal_legacy_migration_config, TemporalLegacyMigrationConfig
    )
    assert config.temporal_legacy_migration_config.relocation_enabled is True
    assert config.temporal_legacy_migration_config.cleanup_authorized is True
