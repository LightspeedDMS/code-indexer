"""Tests for Story #1458 (Epic #1454) item #10: Fleet Migration Web UI
Config route layer.

Covers:
  - "fleet_migration" membership in _VALID_CONFIG_SECTIONS (otherwise every
    POST /admin/config/fleet_migration returns HTTP 400 "Invalid section").
  - _validate_config_section("fleet_migration", ...): rejects
    tick_interval_minutes < 1.
  - _get_current_config() surfaces the "fleet_migration" section.

The ConfigService-level persistence round trip (update_setting() actually
saves the value) is covered separately in
tests/unit/server/services/test_fleet_migration_web_config_1458.py -- this
file covers only the route-layer wiring (section registration + validation),
without mocking any of the route module's own internal helpers.
"""

import unittest.mock as mock


# ---------------------------------------------------------------------------
# Section membership
# ---------------------------------------------------------------------------


class TestFleetMigrationSectionMembership:
    def test_fleet_migration_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "fleet_migration" in _VALID_CONFIG_SECTIONS, (
            "fleet_migration must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/fleet_migration would otherwise always "
            "return HTTP 400 'Invalid section: fleet_migration'."
        )


# ---------------------------------------------------------------------------
# _validate_config_section validation logic
# ---------------------------------------------------------------------------


def _validate(data: dict):
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("fleet_migration", data)


class TestValidateConfigSectionAcceptsValidInput:
    def test_valid_full_payload_returns_none(self) -> None:
        assert _validate({"enabled": True, "tick_interval_minutes": 45}) is None

    def test_missing_fields_returns_none(self) -> None:
        # Partial save must not error.
        assert _validate({}) is None


class TestValidateConfigSectionRejectsInvalidTickInterval:
    def test_tick_interval_minutes_zero_rejected(self) -> None:
        error = _validate({"tick_interval_minutes": 0})
        assert error is not None

    def test_tick_interval_minutes_negative_rejected(self) -> None:
        error = _validate({"tick_interval_minutes": -5})
        assert error is not None


# ---------------------------------------------------------------------------
# _get_current_config() surfaces the section
# ---------------------------------------------------------------------------


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


class TestGetCurrentConfigSurfacesFleetMigration:
    def test_section_key_present(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        assert "fleet_migration" in result

    def test_section_has_default_values(self, tmp_path) -> None:
        svc = _make_service(str(tmp_path))
        result = _call_get_current_config(svc)
        section = result["fleet_migration"]
        assert section["enabled"] is False
        assert section["tick_interval_minutes"] == 30
