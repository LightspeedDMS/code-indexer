"""Tests for Story #1458 (Epic #1454) item #10: Fleet Migration Web UI
Config section -- ConfigService persistence layer (get_all_settings()/
update_setting()).

Mirrors tests/unit/server/services/test_hnsw_orphan_sweep_web_config_1397.py's
pattern exactly. Codex round-6 review: the FleetMigrationConfig docstring and
lifespan.py comments previously (correctly, at the time) documented that this
opt-in was NOT YET exposed as a Web UI Config Screen section -- this closes
that gap for real, mirroring the established HNSWOrphanRepairSweepConfig
pattern, per this project's "No Environment Variables for Server Settings"
rule (runtime settings belong in the Web UI Config Screen via
get_config_service().get_config()).
"""

import pytest


def _make_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    mgr.save_config(mgr.create_default_config())
    return ConfigService(config_manager=mgr)


class TestGetAllSettingsFleetMigrationSection:
    def test_section_present(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        settings = svc.get_all_settings()
        assert "fleet_migration" in settings

    def test_section_has_both_keys(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        section = svc.get_all_settings()["fleet_migration"]
        for key in ("enabled", "tick_interval_minutes"):
            assert key in section, f"Missing key: {key}"

    def test_section_default_values(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        section = svc.get_all_settings()["fleet_migration"]
        # FleetMigrationConfig defaults: enabled=False (deliberate --
        # this scheduler deletes real on-disk chunk data), 30-minute
        # tick interval.
        assert section["enabled"] is False
        assert section["tick_interval_minutes"] == 30


class TestUpdateSettingEnabledCheckboxTrap:
    """Mirrors Story #1397's gotcha #1: the scheduler must be turnable
    ON/OFF via an explicit true/false <select>, not a checkbox -- and
    "false" must persist False, not silently no-op."""

    def test_update_enabled_true_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("fleet_migration", "enabled", "true")
        assert svc.get_config().fleet_migration_config.enabled is True

    def test_update_enabled_false_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("fleet_migration", "enabled", "true")
        svc.update_setting("fleet_migration", "enabled", "false")
        assert svc.get_config().fleet_migration_config.enabled is False


class TestUpdateSettingTickIntervalField:
    def test_update_tick_interval_minutes_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("fleet_migration", "tick_interval_minutes", "45")
        assert svc.get_config().fleet_migration_config.tick_interval_minutes == 45


class TestUpdateSettingUnknownKeyRejected:
    def test_unknown_key_raises_value_error(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        with pytest.raises(ValueError):
            svc.update_setting("fleet_migration", "not_a_real_key", "99")
