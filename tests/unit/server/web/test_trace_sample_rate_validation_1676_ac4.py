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
