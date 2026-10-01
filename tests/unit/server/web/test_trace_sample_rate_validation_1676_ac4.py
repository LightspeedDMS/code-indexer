"""
TDD tests for Story #1676 AC4: configurable trace sampling -- Web UI layer.

Covers:
  - _validate_config_section("telemetry", ...) rejects (never clamps) an
    out-of-range trace_sample_rate submitted via the Config Screen form
  - trace_sample_rate is listed in RESTART_REQUIRED_FIELDS (it is captured
    once into the TracerProvider at startup, same as its telemetry.*
    siblings)
  - ConfigService._update_telemetry_setting applies trace_sample_rate with
    float conversion (the generic update_settings_atomic dispatch path)

All tests exercise the real routes.py/config_service.py code -- no
mocking, per MESSI Rule #1.
"""

import pytest

from code_indexer.server.utils.config_manager import ServerConfig, TelemetryConfig
from code_indexer.server.web.routes import (
    RESTART_REQUIRED_FIELDS,
    _validate_config_section,
)


class TestTraceSampleRateFormValidationRejects:
    """AC4: Web UI form-handling path rejects out-of-range values."""

    @pytest.mark.parametrize("raw_value", ["-0.1", "1.5", "2", "-1"])
    def test_out_of_range_value_rejected(self, raw_value):
        error = _validate_config_section("telemetry", {"trace_sample_rate": raw_value})
        assert error is not None
        assert "trace_sample_rate" in error.lower() or "sample rate" in error.lower()

    def test_non_numeric_value_rejected(self):
        error = _validate_config_section(
            "telemetry", {"trace_sample_rate": "not-a-number"}
        )
        assert error is not None


class TestTraceSampleRateFormValidationAccepts:
    """AC4: Web UI form-handling path accepts in-range values."""

    @pytest.mark.parametrize("raw_value", ["0", "0.1", "0.5", "1", "1.0"])
    def test_in_range_value_accepted(self, raw_value):
        error = _validate_config_section("telemetry", {"trace_sample_rate": raw_value})
        assert error is None


class TestTraceSampleRateRestartRequired:
    """AC4: trace_sample_rate is captured once at startup, same as its
    telemetry.* siblings (collector_protocol, machine_metrics_interval_seconds,
    etc.) -- it must be listed in RESTART_REQUIRED_FIELDS."""

    def test_trace_sample_rate_in_restart_required_fields(self):
        assert "trace_sample_rate" in RESTART_REQUIRED_FIELDS


class TestConfigServiceGetAllSettingsIncludesTraceSampleRate:
    """AC4 round-2 fix (code review Finding 1, HIGH): trace_sample_rate was
    added to config_section.html's template but never added to
    ConfigService.get_all_settings()'s "telemetry" dict -- every OTHER
    telemetry field is listed there, this one was missing. Since the
    Config Screen renders form values FROM this dict, the missing key
    made Jinja2 render an empty string for the field, and since HTML
    forms submit every named input, saving ANY telemetry setting then
    posted trace_sample_rate="" and got rejected with HTTP 400 -- an
    operator could no longer save ANY telemetry setting through the Web
    UI, not just the new field."""

    def test_get_all_settings_telemetry_includes_trace_sample_rate(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        telemetry_settings = service.get_all_settings()["telemetry"]

        assert "trace_sample_rate" in telemetry_settings
        assert telemetry_settings["trace_sample_rate"] == 1.0

    def test_get_all_settings_reflects_updated_trace_sample_rate(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("telemetry", "trace_sample_rate", "0.33")

        telemetry_settings = service.get_all_settings()["telemetry"]
        assert telemetry_settings["trace_sample_rate"] == 0.33


class TestConfigServiceTelemetrySettingApplication:
    """AC4: the generic _apply_setting -> _update_telemetry_setting dispatch
    path actually applies trace_sample_rate with float conversion."""

    def test_update_telemetry_setting_applies_trace_sample_rate(self):
        from code_indexer.server.services.config_service import ConfigService

        config = ServerConfig(server_dir="/tmp/test")
        config.telemetry_config = TelemetryConfig()
        service = ConfigService.__new__(ConfigService)

        service._update_telemetry_setting(config, "trace_sample_rate", "0.42")

        assert config.telemetry_config.trace_sample_rate == 0.42


class TestTelemetryFullFormRoundTrip:
    """AC4 round-2 fix (code review Finding 1 remediation): a real HTML
    form submits EVERY named input, not just the field the operator
    changed. This builds the exact ~10-key telemetry form dict --
    sourced from get_all_settings(), exactly how config_section.html
    populates each field's initial value -- submits it through the real
    _validate_config_section -> ConfigService.update_settings_atomic
    path, and asserts the save succeeds and every field persists. This
    is the regression class Finding 1 exposed: a field present in the
    template but missing from get_all_settings() would KeyError/render
    blank here, and a blank value fails float() validation and rejects
    the WHOLE section save -- not just the one new field."""

    _TELEMETRY_FORM_FIELDS = [
        "enabled",
        "collector_endpoint",
        "collector_protocol",
        "service_name",
        "export_traces",
        "export_metrics",
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

    def test_full_form_round_trip_persists_changed_trace_sample_rate(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        current = service.get_all_settings()["telemetry"]
        form_data = self._build_full_form_data(current)
        form_data["trace_sample_rate"] = "0.3"  # operator changes only this field

        error = _validate_config_section("telemetry", form_data)
        assert error is None, f"Full-form submission unexpectedly rejected: {error}"

        updates = [("telemetry", key, value) for key, value in form_data.items()]
        service.update_settings_atomic(updates)

        assert service.get_config().telemetry_config.trace_sample_rate == 0.3

    def test_full_form_round_trip_preserves_untouched_sibling_fields(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

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
