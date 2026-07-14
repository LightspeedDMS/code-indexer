"""Tests for Story #1397: HNSW orphan-repair sweep Web UI Config section --
ConfigService persistence layer (get_all_settings()/update_setting()).

Mirrors tests/unit/server/services/test_activated_reaper_config_967.py's
pattern. Written BEFORE the ConfigService plumbing exists (TDD RED phase);
all tests are expected to fail until the "hnsw_orphan_sweep" category branch
is added to get_all_settings() and update_setting().

Covers the exact gap #1397 calls out: a prior narrower story (#1395) missed
this ConfigService layer entirely, which would make the Web UI POST fail at
runtime (ValueError: Unknown category) even with the route layer wired.
"""


def _make_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    mgr.save_config(mgr.create_default_config())
    return ConfigService(config_manager=mgr)


class TestGetAllSettingsHnswOrphanSweepSection:
    def test_section_present(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        settings = svc.get_all_settings()
        assert "hnsw_orphan_sweep" in settings

    def test_section_has_all_five_keys(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        section = svc.get_all_settings()["hnsw_orphan_sweep"]
        for key in (
            "enabled",
            "batch_size",
            "tick_interval_minutes",
            "operating_hours_start_utc",
            "operating_hours_end_utc",
        ):
            assert key in section, f"Missing key: {key}"

    def test_section_default_values(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        section = svc.get_all_settings()["hnsw_orphan_sweep"]
        assert section["enabled"] is True
        assert section["batch_size"] == 15
        assert section["tick_interval_minutes"] == 7
        assert section["operating_hours_start_utc"] == 0
        assert section["operating_hours_end_utc"] == 0


class TestUpdateSettingEnabledCheckboxTrap:
    """Story #1397 gotcha #1: the sweep must be turnable OFF, i.e.
    update_setting("hnsw_orphan_sweep", "enabled", "false") must persist
    False, not silently no-op."""

    def test_update_enabled_true_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("hnsw_orphan_sweep", "enabled", "true")
        assert svc.get_config().hnsw_orphan_repair_sweep_config.enabled is True

    def test_update_enabled_false_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("hnsw_orphan_sweep", "enabled", "false")
        assert svc.get_config().hnsw_orphan_repair_sweep_config.enabled is False


class TestUpdateSettingNumericFields:
    def test_update_batch_size_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("hnsw_orphan_sweep", "batch_size", "10")
        assert svc.get_config().hnsw_orphan_repair_sweep_config.batch_size == 10

    def test_update_tick_interval_minutes_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("hnsw_orphan_sweep", "tick_interval_minutes", "20")
        assert (
            svc.get_config().hnsw_orphan_repair_sweep_config.tick_interval_minutes == 20
        )


class TestUpdateSettingOperatingHoursFields:
    def test_update_start_utc_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("hnsw_orphan_sweep", "operating_hours_start_utc", "22")
        assert (
            svc.get_config().hnsw_orphan_repair_sweep_config.operating_hours_start_utc
            == 22
        )

    def test_update_end_utc_persists(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        svc.update_setting("hnsw_orphan_sweep", "operating_hours_end_utc", "6")
        assert (
            svc.get_config().hnsw_orphan_repair_sweep_config.operating_hours_end_utc
            == 6
        )


class TestUpdateSettingUnknownKeyRejected:
    def test_unknown_key_raises_value_error(self, tmp_path) -> None:
        import pytest

        svc = _make_service(str(tmp_path))
        with pytest.raises(ValueError):
            svc.update_setting("hnsw_orphan_sweep", "not_a_real_key", "99")
