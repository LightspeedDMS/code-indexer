"""
TDD tests for Story #1676 AC3: real OTLP Logs export -- Web UI / ConfigService
wiring.

Mirrors exactly the pattern Story #1676 AC4 established for
trace_sample_rate (see test_trace_sample_rate_validation_1676_ac4.py and its
own docstring about the round-2 "half-wired field" defect: a field present
in config_section.html's template but missing from
ConfigService.get_all_settings() renders blank, and a blank value posted
back through a FULL form submission then rejects the WHOLE section save,
not just the new field). Requirement 9 explicitly calls out avoiding a
repeat of that exact mistake for export_logs.

Covers:
  - export_logs is listed in RESTART_REQUIRED_FIELDS (captured once into
    the LoggerProvider/log bridge handler at startup, same lifecycle as its
    telemetry.* siblings)
  - ConfigService.get_all_settings()["telemetry"] includes export_logs
  - ConfigService._update_telemetry_setting applies export_logs with bool
    conversion (the generic update_settings_atomic dispatch path)
  - A full ~11-key telemetry form submission (mirroring exactly what
    config_section.html renders) round-trips export_logs and preserves
    every untouched sibling field

All tests exercise the real routes.py/config_service.py code -- no
mocking, per MESSI Rule #1.
"""

from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS


class TestExportLogsRestartRequired:
    """AC3: export_logs is captured once at startup, same as its
    telemetry.* siblings -- it must be listed in RESTART_REQUIRED_FIELDS."""

    def test_export_logs_in_restart_required_fields(self):
        assert "export_logs" in RESTART_REQUIRED_FIELDS


class TestConfigServiceGetAllSettingsIncludesExportLogs:
    def test_get_all_settings_telemetry_includes_export_logs(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        telemetry_settings = service.get_all_settings()["telemetry"]

        assert "export_logs" in telemetry_settings
        assert telemetry_settings["export_logs"] is False

    def test_get_all_settings_reflects_updated_export_logs(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("telemetry", "export_logs", "true")

        telemetry_settings = service.get_all_settings()["telemetry"]
        assert telemetry_settings["export_logs"] is True


class TestConfigServiceTelemetrySettingApplication:
    """AC3: the generic _apply_setting -> _update_telemetry_setting dispatch
    path actually applies export_logs with bool conversion."""

    def test_update_telemetry_setting_applies_export_logs(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            TelemetryConfig,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.telemetry_config = TelemetryConfig()
        service = ConfigService.__new__(ConfigService)

        service._update_telemetry_setting(config, "export_logs", "true")

        assert config.telemetry_config.export_logs is True


class TestTelemetryFullFormRoundTrip:
    """AC3: a real HTML form submits EVERY named input, not just the field
    the operator changed. This builds the full telemetry form dict --
    sourced from get_all_settings(), exactly how config_section.html
    populates each field's initial value -- submits it through the real
    _validate_config_section -> ConfigService.update_settings_atomic path,
    and asserts the save succeeds and every field persists, including the
    newly-added export_logs."""

    _TELEMETRY_FORM_FIELDS = [
        "enabled",
        "collector_endpoint",
        "collector_protocol",
        "service_name",
        "export_traces",
        "export_metrics",
        "export_logs",
        "machine_metrics_enabled",
        "machine_metrics_interval_seconds",
        "trace_sample_rate",
        "deployment_environment",
    ]

    def _build_full_form_data(self, telemetry_settings: dict) -> dict:
        """Mirror config_section.html's rendering: a boolean field's
        <select> submits the literal string "true"/"false" (the <option
        value="..."> the template marks selected); everything else
        submits Python's str() of the underlying value -- exactly what
        Jinja2 substitutes into value="{{ ... }}"."""
        form_data = {}
        for field in self._TELEMETRY_FORM_FIELDS:
            value = telemetry_settings[field]
            form_data[field] = (
                "true" if value is True else ("false" if value is False else str(value))
            )
        return form_data

    def test_full_form_round_trip_persists_changed_export_logs(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.web.routes import _validate_config_section

        service = ConfigService(server_dir_path=str(tmp_path))
        current = service.get_all_settings()["telemetry"]
        form_data = self._build_full_form_data(current)
        form_data["export_logs"] = "true"  # operator changes only this field

        error = _validate_config_section("telemetry", form_data)
        assert error is None, f"Full-form submission unexpectedly rejected: {error}"

        updates = [("telemetry", key, value) for key, value in form_data.items()]
        service.update_settings_atomic(updates)

        assert service.get_config().telemetry_config.export_logs is True

    def test_full_form_round_trip_preserves_untouched_sibling_fields(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.web.routes import _validate_config_section

        service = ConfigService(server_dir_path=str(tmp_path))
        current = service.get_all_settings()["telemetry"]
        form_data = self._build_full_form_data(current)

        error = _validate_config_section("telemetry", form_data)
        assert error is None, f"Full-form submission unexpectedly rejected: {error}"

        updates = [("telemetry", key, value) for key, value in form_data.items()]
        service.update_settings_atomic(updates)

        telemetry = service.get_config().telemetry_config
        for field in self._TELEMETRY_FORM_FIELDS:
            assert getattr(telemetry, field) == current[field], (
                f"Field {field!r} did not round-trip: expected "
                f"{current[field]!r}, got {getattr(telemetry, field)!r}"
            )
