"""
TDD tests for Story #1676 AC4: configurable trace sampling -- config layer.

Covers the TelemetryConfig.trace_sample_rate field itself:
  - default value (1.0, preserving pre-AC4 always-on behavior for operators
    who never touch the setting)
  - backend validation rejects (never clamps) an out-of-range value
  - the value survives a save/load round-trip instead of being silently
    stripped by the Bug #938 dead-field-stripping code path (which used to
    treat trace_sample_rate as dead; AC4 resurrects it as a real, live
    field)

All tests use the real ServerConfigManager/TelemetryConfig dataclasses --
no mocking, per MESSI Rule #1.
"""

import json
import tempfile

import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    TelemetryConfig,
)


def _config_with_rate(rate: float) -> ServerConfig:
    """Build a ServerConfig with telemetry_config.trace_sample_rate=rate."""
    config = ServerConfig(server_dir="/tmp/test")
    config.telemetry_config = TelemetryConfig(trace_sample_rate=rate)
    return config


class TestTraceSampleRateDefault:
    """AC4: trace_sample_rate defaults to 1.0 (pure opt-in addition)."""

    def test_default_trace_sample_rate_is_one(self):
        config = TelemetryConfig()
        assert config.trace_sample_rate == 1.0

    def test_serverconfig_default_trace_sample_rate_is_one(self):
        config = ServerConfig(server_dir="/tmp/test")
        assert config.telemetry_config.trace_sample_rate == 1.0  # type: ignore[union-attr]


class TestTraceSampleRateValidationAccepts:
    """AC4: backend validate_config accepts in-range trace_sample_rate."""

    @pytest.mark.parametrize("rate", [0.0, 0.1, 1.0])
    def test_validation_accepts_in_range_rate(self, rate):
        config = _config_with_rate(rate)
        manager = ServerConfigManager("/tmp/test")
        manager.validate_config(config)  # should not raise


class TestTraceSampleRateValidationRejects:
    """AC4: backend validate_config rejects out-of-range trace_sample_rate."""

    @pytest.mark.parametrize("rate", [-0.1, 1.5])
    def test_validation_rejects_out_of_range_rate(self, rate):
        config = _config_with_rate(rate)
        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert "trace_sample_rate" in str(exc_info.value).lower()

    def test_validation_does_not_clamp_out_of_range_value(self):
        """Rejection, not silent clamping -- the invalid value must still be
        sitting on the config object after validate_config raises."""
        config = _config_with_rate(2.5)
        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError):
            manager.validate_config(config)

        assert config.telemetry_config.trace_sample_rate == 2.5  # type: ignore[union-attr]


class TestTraceSampleRateRoundTrip:
    """AC4: trace_sample_rate survives save/load, never silently stripped.

    Bug #938 introduced a stripping step for trace_sample_rate (it was dead
    at the time). AC4 resurrects it as a real field -- the stripping code
    must be updated to stop discarding it, or every config load/save cycle
    silently deletes the operator's configured sample rate.
    """

    def test_trace_sample_rate_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            original = ServerConfig(server_dir=tmpdir)
            original.telemetry_config = TelemetryConfig(trace_sample_rate=0.25)

            manager.save_config(original)
            loaded = manager.load_config()

            assert loaded is not None
            assert loaded.telemetry_config.trace_sample_rate == 0.25  # type: ignore[union-attr]

    def test_trace_sample_rate_present_in_serialized_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            config = ServerConfig(server_dir=tmpdir)
            config.telemetry_config = TelemetryConfig(trace_sample_rate=0.42)

            manager.save_config(config)

            with open(manager.config_file_path, "r") as f:
                config_dict = json.load(f)

            assert config_dict["telemetry_config"]["trace_sample_rate"] == 0.42

    def test_trace_sample_rate_survives_load_reload_cycle(self):
        """Loading a config.json that already has trace_sample_rate set
        (e.g. written by a previous server version that already has AC4)
        must not lose the value on the very next load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            config_dict = {
                "server_dir": tmpdir,
                "telemetry_config": {
                    "enabled": True,
                    "trace_sample_rate": 0.33,
                },
            }
            with open(manager.config_file_path, "w") as f:
                json.dump(config_dict, f)

            loaded = manager.load_config()

            assert loaded is not None
            assert loaded.telemetry_config.trace_sample_rate == 0.33  # type: ignore[union-attr]
