"""Tests for ConfigService wiring of EmbeddingStatsConfig (Story #1418 Phase 3).

Mirrors the get_all_settings() / update_setting() wiring pattern established
for search_timeouts (Issue #1398): an "embedding_stats" category dict read
helper wired into get_all_settings(), and an "elif category ==
'embedding_stats':" branch in update_setting() dispatching to a
_update_embedding_stats_setting() write helper.
"""

from code_indexer.server.services.config_service import ConfigService


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


class TestGetAllSettingsSurfacesEmbeddingStats:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        settings = svc.get_all_settings()
        assert "embedding_stats" in settings

    def test_section_has_default_values(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        section = svc.get_all_settings()["embedding_stats"]
        assert section["enabled"] is True
        assert section["flush_interval_seconds"] == 30.0
        assert section["retention_days"] == 90


class TestUpdateSettingEmbeddingStats:
    def test_update_enabled(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("embedding_stats", "enabled", False)
        assert svc.get_config().embedding_stats_config.enabled is False

    def test_update_enabled_with_string_false_value(self, tmp_path) -> None:
        """Bug: the Web UI config form posts the string "false" (not the
        native bool False) for a disabled checkbox/select. bool("false")
        evaluates to True in Python (any non-empty string is truthy), so
        the bare `bool(value)` coercion previously used in
        _update_embedding_stats_setting could never actually disable the
        kill-switch via the front door. Must use _parse_bool like sibling
        boolean settings (coalesce_enabled, memory_governor_enabled)."""
        svc = _make_service(tmp_path)
        svc.update_setting("embedding_stats", "enabled", "false")
        assert svc.get_config().embedding_stats_config.enabled is False

    def test_update_enabled_with_string_true_value(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("embedding_stats", "enabled", "true")
        assert svc.get_config().embedding_stats_config.enabled is True

    def test_update_flush_interval_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("embedding_stats", "flush_interval_seconds", 45.0)
        assert svc.get_config().embedding_stats_config.flush_interval_seconds == 45.0

    def test_update_retention_days(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("embedding_stats", "retention_days", 30)
        assert svc.get_config().embedding_stats_config.retention_days == 30

    def test_unknown_key_raises_value_error(self, tmp_path) -> None:
        import pytest

        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("embedding_stats", "not_a_real_field", 1)

    def test_out_of_range_value_raises_value_error_on_save(self, tmp_path) -> None:
        """update_setting validates via config_manager.validate_config()
        before saving (skip_validation defaults to False)."""
        import pytest

        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("embedding_stats", "retention_days", 0)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
