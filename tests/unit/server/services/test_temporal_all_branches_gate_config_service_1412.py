"""Unit tests for Story #1412 - config_service wiring of
IndexingConfig.temporal_all_branches_enabled.

Mirrors the Story #1158 temporal_parallel_requests wiring test pattern
(tests/unit/server/test_parallel_requests_config_1158.py).

Section 1: get_config() 'indexing' dict display.
Section 2: _update_indexing_setting() key handling.
Section 3: get_all_settings() round-trip.
"""

import tempfile

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfigManager


def _make_service() -> ConfigService:
    """Create a ConfigService instance backed by real SQLite via temp dir."""
    tmp = tempfile.mkdtemp()
    mgr = ServerConfigManager(server_dir_path=tmp)
    return ConfigService(config_manager=mgr)


class TestGetConfigDisplaysTemporalAllBranchesEnabled:
    """AC1/AC8: get_config().indexing_config must expose the gate; display
    plumbing in ConfigService.get_all_settings() must surface it too."""

    def test_get_all_settings_includes_temporal_all_branches_enabled(self) -> None:
        svc = _make_service()
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert "temporal_all_branches_enabled" in indexing, (
            "temporal_all_branches_enabled missing from get_all_settings()['indexing']"
        )

    def test_get_all_settings_default_is_false(self) -> None:
        svc = _make_service()
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert indexing["temporal_all_branches_enabled"] is False


class TestUpdateIndexingSettingTemporalAllBranchesEnabled:
    """_update_indexing_setting() must handle temporal_all_branches_enabled."""

    def test_update_true_string_sets_true(self) -> None:
        svc = _make_service()
        svc._update_indexing_setting("temporal_all_branches_enabled", "true")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_all_branches_enabled is True

    def test_update_false_string_sets_false(self) -> None:
        svc = _make_service()
        svc._update_indexing_setting("temporal_all_branches_enabled", "true")
        svc._update_indexing_setting("temporal_all_branches_enabled", "false")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_all_branches_enabled is False

    def test_update_bool_true_passthrough(self) -> None:
        svc = _make_service()
        svc._update_indexing_setting("temporal_all_branches_enabled", True)
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_all_branches_enabled is True

    def test_update_bool_false_passthrough(self) -> None:
        svc = _make_service()
        svc._update_indexing_setting("temporal_all_branches_enabled", False)
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_all_branches_enabled is False

    def test_update_persists_and_reflects_in_get_all_settings(self) -> None:
        svc = _make_service()
        svc._update_indexing_setting("temporal_all_branches_enabled", "true")
        settings = svc.get_all_settings()
        assert settings["indexing"]["temporal_all_branches_enabled"] is True


class TestValidateConfigSectionIndexingTemporalAllBranchesEnabled:
    """_validate_config_section('indexing', ...) must not reject valid values."""

    def _validate(self, data):
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("indexing", data)

    @pytest.mark.parametrize("raw", ["true", "false", "on", "off"])
    def test_valid_values_pass(self, raw) -> None:
        result = self._validate({"temporal_all_branches_enabled": raw})
        assert result is None
