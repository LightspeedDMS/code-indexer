"""
Backward-compatibility tests for Bug #938 dead-field removal.

Verifies that config.json / DB rows written by older server versions that
still contain the 5 removed fields load cleanly without raising TypeError.

Fields removed in Bug #938:
- TelemetryConfig.export_logs
- TelemetryConfig.trace_sample_rate
- HealthConfig.system_metrics_cache_ttl_seconds
- MultiSearchLimitsConfig.omni_max_total_results_before_aggregation
- ContentLimitsConfig.cache_max_entries
"""

import json
import tempfile

import pytest

from src.code_indexer.server.utils.config_manager import ServerConfigManager


def _write_and_load(tmpdir: str, config_dict: dict):
    """Write config dict to file and load it via ServerConfigManager."""
    manager = ServerConfigManager(tmpdir)
    with open(manager.config_file_path, "w") as f:
        json.dump(config_dict, f)
    return manager.load_config()


# Each tuple: (section_key, dead_field, dead_value, config_attr_path)
# config_attr_path is a tuple of attribute names to traverse from the loaded config.
_DEAD_FIELD_CASES = [
    (
        "telemetry_config",
        "export_logs",
        True,
        ("telemetry_config", "export_logs"),
    ),
    (
        "telemetry_config",
        "trace_sample_rate",
        0.5,
        ("telemetry_config", "trace_sample_rate"),
    ),
    (
        "health_config",
        "system_metrics_cache_ttl_seconds",
        10,
        ("health_config", "system_metrics_cache_ttl_seconds"),
    ),
    (
        "multi_search_limits_config",
        "omni_max_total_results_before_aggregation",
        50000,
        ("multi_search_limits_config", "omni_max_total_results_before_aggregation"),
    ),
    (
        "content_limits_config",
        "cache_max_entries",
        5000,
        ("content_limits_config", "cache_max_entries"),
    ),
]


class TestBug938BackwardCompat:
    """AC5: Old config keys must load cleanly without raising."""

    @pytest.mark.parametrize(
        "section_key, dead_field, dead_value, attr_path",
        _DEAD_FIELD_CASES,
        ids=[case[1] for case in _DEAD_FIELD_CASES],
    )
    def test_dead_field_loads_cleanly(
        self, section_key, dead_field, dead_value, attr_path
    ):
        """
        Old config containing a removed field must load without TypeError.

        Given a config.json with a dead field in the relevant sub-section
        When loaded via ServerConfigManager
        Then it loads successfully and the dead field is not present on the result
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dict = {
                "server_dir": tmpdir,
                section_key: {dead_field: dead_value},
            }
            config = _write_and_load(tmpdir, config_dict)
            assert config is not None
            # Dead field must not survive onto the loaded dataclass
            obj = config
            for attr in attr_path[:-1]:
                obj = getattr(obj, attr)
            assert not hasattr(obj, attr_path[-1])

    def test_all_five_dead_keys_together_load_cleanly(self):
        """
        Old config with all 5 dead keys present simultaneously must not raise.

        Given a config.json containing all 5 removed fields across their sections
        When loaded via ServerConfigManager
        Then it loads without TypeError
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dict = {
                "server_dir": tmpdir,
                "telemetry_config": {
                    "enabled": False,
                    "export_logs": False,
                    "trace_sample_rate": 1.0,
                },
                "health_config": {
                    "memory_warning_threshold_percent": 80.0,
                    "system_metrics_cache_ttl_seconds": 5,
                },
                "multi_search_limits_config": {
                    "omni_max_total_results_before_aggregation": 10000
                },
                "content_limits_config": {
                    "cache_ttl_seconds": 3600,
                    "cache_max_entries": 10000,
                },
            }
            config = _write_and_load(tmpdir, config_dict)
            assert config is not None
