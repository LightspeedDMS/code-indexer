"""Tests for ConfigService wiring of TemporalIndexingConfig (Story #1404).

Mirrors the get_all_settings() / update_setting() wiring pattern established
for search_timeouts (Issue #1398) / embedding_stats (Story #1418): a
"temporal_indexing" category dict read helper wired into get_all_settings(),
and an "elif category == 'temporal_indexing':" branch in update_setting()
dispatching to a _update_temporal_indexing_setting() write helper.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


class TestGetAllSettingsSurfacesTemporalIndexing:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        settings = svc.get_all_settings()
        assert "temporal_indexing" in settings

    def test_section_has_default_values(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        section = svc.get_all_settings()["temporal_indexing"]
        assert section["index_floor_date"] is None


class TestUpdateSettingTemporalIndexing:
    def test_update_index_floor_date(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("temporal_indexing", "index_floor_date", "2025-01-01")
        assert (
            svc.get_config().temporal_indexing_config.index_floor_date == "2025-01-01"
        )

    def test_clear_index_floor_date_via_empty_string(self, tmp_path) -> None:
        """None/empty is the documented safety no-op (unbounded, full
        history) -- clearing a previously-set floor date must be possible
        via the same field, not a separate unset endpoint."""
        svc = _make_service(tmp_path)
        svc.update_setting("temporal_indexing", "index_floor_date", "2025-01-01")
        svc.update_setting("temporal_indexing", "index_floor_date", "")
        assert svc.get_config().temporal_indexing_config.index_floor_date == ""

    def test_unknown_key_raises_value_error(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("temporal_indexing", "not_a_real_field", "x")

    def test_malformed_value_raises_value_error_on_save(self, tmp_path) -> None:
        """update_setting validates via config_manager.validate_config()
        before saving (skip_validation defaults to False)."""
        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("temporal_indexing", "index_floor_date", "2026-02-30")

    def test_previous_value_preserved_after_rejected_update(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("temporal_indexing", "index_floor_date", "2025-01-01")
        with pytest.raises(ValueError):
            svc.update_setting("temporal_indexing", "index_floor_date", "not-a-date")
        assert (
            svc.get_config().temporal_indexing_config.index_floor_date == "2025-01-01"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
