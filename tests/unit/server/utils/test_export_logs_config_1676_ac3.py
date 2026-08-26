"""
TDD tests for Story #1676 AC3: real OTLP Logs export -- config layer.

Covers the TelemetryConfig.export_logs field itself:
  - default value (False, so a fresh install produces zero OTLP log
    traffic and constructs no logging provider at all)
  - the value survives a save/load round-trip instead of being silently
    stripped by the Bug #938 dead-field-stripping code path (which
    stripped export_logs pending AC3 -- this story resurrects it as a
    real, live field, mirroring exactly how AC4 resurrected
    trace_sample_rate)

All tests use the real ServerConfigManager/TelemetryConfig dataclasses --
no mocking, per MESSI Rule #1.
"""

import json
import tempfile

from src.code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    TelemetryConfig,
)


class TestExportLogsDefault:
    """AC3: export_logs defaults to False (pure opt-in addition)."""

    def test_default_export_logs_is_false(self):
        config = TelemetryConfig()
        assert config.export_logs is False

    def test_serverconfig_default_export_logs_is_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = ServerConfig(server_dir=tmpdir)
            assert config.telemetry_config.export_logs is False  # type: ignore[union-attr]


class TestExportLogsRoundTrip:
    """AC3: export_logs survives save/load, never silently stripped.

    Bug #938 introduced a stripping step for export_logs (dead pending
    AC3). This story resurrects it as a real field -- the stripping code
    must be updated to stop discarding it, or every config load/save
    cycle silently deletes the operator's configured value.
    """

    def test_export_logs_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            original = ServerConfig(server_dir=tmpdir)
            original.telemetry_config = TelemetryConfig(export_logs=True)

            manager.save_config(original)
            loaded = manager.load_config()

            assert loaded is not None
            assert loaded.telemetry_config.export_logs is True  # type: ignore[union-attr]

    def test_export_logs_present_in_serialized_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            config = ServerConfig(server_dir=tmpdir)
            config.telemetry_config = TelemetryConfig(export_logs=True)

            manager.save_config(config)

            with open(manager.config_file_path, "r") as f:
                config_dict = json.load(f)

            assert config_dict["telemetry_config"]["export_logs"] is True

    def test_export_logs_survives_load_reload_cycle(self):
        """Loading a config.json that already has export_logs set (e.g.
        written by a previous server version that already has AC3), then
        saving and loading it AGAIN, must not lose the value on either
        pass through the stripping code path in _dict_to_server_config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            config_dict = {
                "server_dir": tmpdir,
                "telemetry_config": {
                    "enabled": True,
                    "export_logs": True,
                },
            }
            with open(manager.config_file_path, "w") as f:
                json.dump(config_dict, f)

            first_load = manager.load_config()
            assert first_load is not None
            assert first_load.telemetry_config.export_logs is True  # type: ignore[union-attr]

            manager.save_config(first_load)
            second_load = manager.load_config()

            assert second_load is not None
            assert second_load.telemetry_config.export_logs is True  # type: ignore[union-attr]
