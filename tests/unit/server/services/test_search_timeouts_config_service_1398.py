"""Tests for ConfigService wiring of SearchTimeoutsConfig (Issue #1398).

Mirrors the get_all_settings() / update_setting() wiring pattern established
for hnsw_orphan_sweep (Story #1397): a "search_timeouts" category dict read
helper wired into get_all_settings(), and an "elif category ==
'search_timeouts':" branch in update_setting() dispatching to a
_update_search_timeouts_setting() write helper.
"""

from code_indexer.server.services.config_service import ConfigService


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


class TestGetAllSettingsSurfacesSearchTimeouts:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        settings = svc.get_all_settings()
        assert "search_timeouts" in settings

    def test_section_has_default_values(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        section = svc.get_all_settings()["search_timeouts"]
        assert section["search_code_handler_timeout_seconds"] == 180
        assert section["default_handler_timeout_seconds"] == 60
        assert section["write_mode_handler_timeout_seconds"] == 720
        assert section["embedding_provider_timeout_seconds"] == 30
        assert section["reranker_timeout_seconds"] == 15
        assert section["rest_query_handler_timeout_seconds"] == 180


class TestUpdateSettingSearchTimeouts:
    def test_update_search_code_handler_timeout_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting(
            "search_timeouts", "search_code_handler_timeout_seconds", 240
        )
        assert (
            svc.get_config().search_timeouts_config.search_code_handler_timeout_seconds
            == 240
        )

    def test_update_default_handler_timeout_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "default_handler_timeout_seconds", 90)
        assert (
            svc.get_config().search_timeouts_config.default_handler_timeout_seconds
            == 90
        )

    def test_update_write_mode_handler_timeout_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "write_mode_handler_timeout_seconds", 900)
        assert (
            svc.get_config().search_timeouts_config.write_mode_handler_timeout_seconds
            == 900
        )

    def test_update_embedding_provider_timeout_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "embedding_provider_timeout_seconds", 45)
        assert (
            svc.get_config().search_timeouts_config.embedding_provider_timeout_seconds
            == 45
        )

    def test_update_reranker_timeout_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "reranker_timeout_seconds", 25)
        assert svc.get_config().search_timeouts_config.reranker_timeout_seconds == 25

    def test_update_rest_query_handler_timeout_seconds(self, tmp_path) -> None:
        """Issue #1435."""
        svc = _make_service(tmp_path)
        svc.update_setting("search_timeouts", "rest_query_handler_timeout_seconds", 240)
        assert (
            svc.get_config().search_timeouts_config.rest_query_handler_timeout_seconds
            == 240
        )

    def test_unknown_key_raises_value_error(self, tmp_path) -> None:
        import pytest

        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("search_timeouts", "not_a_real_field", 1)

    def test_out_of_range_value_raises_value_error_on_save(self, tmp_path) -> None:
        """update_setting validates via config_manager.validate_config()
        before saving (skip_validation defaults to False)."""
        import pytest

        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting(
                "search_timeouts", "search_code_handler_timeout_seconds", 1
            )


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
