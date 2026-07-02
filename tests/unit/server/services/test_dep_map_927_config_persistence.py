"""Story #927 CRITICAL #1: ConfigService persistence for dep_map_auto_repair_enabled."""

from __future__ import annotations

from pathlib import Path


def _make_config_service(tmp_dir: Path):
    """Create a ConfigService backed by a temp directory."""
    from code_indexer.server.services.config_service import ConfigService

    return ConfigService(server_dir_path=str(tmp_dir))


class TestRoundTripPersistence:
    """Write then read must reflect the written value."""

    def test_round_trip_true(self, tmp_path):
        svc = _make_config_service(tmp_path)
        svc.update_setting("claude_cli", "dep_map_auto_repair_enabled", "true")
        assert (
            svc.get_all_settings()["claude_cli"]["dep_map_auto_repair_enabled"] is True
        )

    def test_round_trip_false_after_true(self, tmp_path):
        svc = _make_config_service(tmp_path)
        svc.update_setting("claude_cli", "dep_map_auto_repair_enabled", "true")
        svc.update_setting("claude_cli", "dep_map_auto_repair_enabled", "false")
        assert (
            svc.get_all_settings()["claude_cli"]["dep_map_auto_repair_enabled"] is False
        )


class TestGetAllSettingsVisibility:
    """get_all_settings must expose dep_map_auto_repair_enabled and dep_map_fact_check_enabled."""

    def test_auto_repair_field_present(self, tmp_path):
        svc = _make_config_service(tmp_path)
        settings = svc.get_all_settings()
        assert "dep_map_auto_repair_enabled" in settings["claude_cli"]

    def test_fact_check_field_present(self, tmp_path):
        svc = _make_config_service(tmp_path)
        settings = svc.get_all_settings()
        assert "dep_map_fact_check_enabled" in settings["claude_cli"]

    def test_auto_repair_default_is_false(self, tmp_path):
        svc = _make_config_service(tmp_path)
        settings = svc.get_all_settings()
        assert settings["claude_cli"]["dep_map_auto_repair_enabled"] is False


class TestUpdateSettingAcceptance:
    """update_setting must accept dep_map_auto_repair_enabled without raising."""

    def test_update_true_does_not_raise(self, tmp_path):
        svc = _make_config_service(tmp_path)
        svc.update_setting("claude_cli", "dep_map_auto_repair_enabled", "true")

    def test_update_false_does_not_raise(self, tmp_path):
        svc = _make_config_service(tmp_path)
        svc.update_setting("claude_cli", "dep_map_auto_repair_enabled", "false")

    def test_update_bool_true_does_not_raise(self, tmp_path):
        svc = _make_config_service(tmp_path)
        svc.update_setting("claude_cli", "dep_map_auto_repair_enabled", True)
