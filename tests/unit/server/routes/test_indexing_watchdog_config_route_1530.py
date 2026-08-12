"""Tests for Issue #1530: Indexing Watchdog Web UI Config route layer.

Mirrors test_search_timeouts_config_route_1398.py's exact structure:

  - "indexing_watchdog" membership in _VALID_CONFIG_SECTIONS (otherwise
    every POST /admin/config/indexing_watchdog returns HTTP 400 "Invalid
    section").
  - _validate_config_section("indexing_watchdog", ...): rejects
    out-of-range/non-numeric values.
  - _get_current_config() surfaces the "indexing_watchdog" section.
"""

import unittest.mock as mock


class TestIndexingWatchdogSectionMembership:
    def test_indexing_watchdog_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "indexing_watchdog" in _VALID_CONFIG_SECTIONS, (
            "indexing_watchdog must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/indexing_watchdog would otherwise always "
            "return HTTP 400 'Invalid section: indexing_watchdog'."
        )


def _validate(data: dict):
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("indexing_watchdog", data)


class TestValidateConfigSectionAcceptsValidInput:
    def test_valid_payload_returns_none(self) -> None:
        assert _validate({"stale_activity_timeout_seconds": 120.0}) is None


class TestValidateConfigSectionRejectsOutOfRange:
    def test_zero_rejected(self) -> None:
        assert _validate({"stale_activity_timeout_seconds": 0.0}) is not None

    def test_negative_rejected(self) -> None:
        assert _validate({"stale_activity_timeout_seconds": -1.0}) is not None

    def test_absurdly_large_rejected(self) -> None:
        assert _validate({"stale_activity_timeout_seconds": 999999.0}) is not None

    def test_non_numeric_value_rejected(self) -> None:
        assert _validate({"stale_activity_timeout_seconds": "not-a-number"}) is not None


def _make_service(tmp_dir: str):
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfigManager

    mgr = ServerConfigManager(server_dir_path=tmp_dir)
    return ConfigService(config_manager=mgr)


def _call_get_current_config(svc) -> dict:
    from code_indexer.server.web import routes

    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        result: dict = routes._get_current_config()
        return result


class TestGetCurrentConfigSurfacesIndexingWatchdog:
    def test_section_key_present_with_default(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert "indexing_watchdog" in result
        assert result["indexing_watchdog"]["stale_activity_timeout_seconds"] == 120.0
