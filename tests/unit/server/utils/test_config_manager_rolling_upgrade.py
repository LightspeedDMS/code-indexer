"""
Story #724 AC12 rolling-upgrade safety tests.

Verifies that ClaudeIntegrationConfig deserialization in
ServerConfigManager._dict_to_server_config tolerates unknown keys. This is
required for cluster rolling-restart safety per CLAUDE.md
"CRITICAL: DATABASE MIGRATIONS MUST BE BACKWARD COMPATIBLE" -- the same
rolling-upgrade constraint applies to JSON-blob deserialization.

Scenario: a new-code node writes a server_config JSON blob containing
additional claude_integration_config keys. An old-code node reads the same
blob at startup. Old code must not crash on the unknown keys.
"""

import pytest

from code_indexer.server.utils.config_manager import ServerConfigManager

# Named constants to avoid magic numbers in assertions
CUSTOM_TIMEOUT_SECONDS = 900
DEFAULT_TIMEOUT_SECONDS = 600
FUTURE_INT_VALUE = 42


def _build_dict_with_unknown_keys(server_dir: str) -> dict:
    """Build a server_config dict that simulates a future-version blob with
    extra keys under claude_integration_config that old code wouldn't recognize."""
    return {
        "server_dir": server_dir,
        "claude_integration_config": {
            # Known fields (should deserialize normally)
            "dep_map_fact_check_enabled": True,
            "fact_check_timeout_seconds": CUSTOM_TIMEOUT_SECONDS,
            # Unknown fields simulating a FUTURE version that added more settings
            "some_future_field_old_code_doesnt_know": "whatever",
            "another_future_field": FUTURE_INT_VALUE,
            "yet_another_nested_thing": {"k": "v"},
        },
    }


@pytest.fixture
def manager(tmp_path):
    """Shared ServerConfigManager instance for rolling-upgrade tests."""
    return ServerConfigManager(str(tmp_path))


class TestRollingUpgradeUnknownKeys:
    """Unknown-key tolerance: new-code blobs must not crash old-code nodes."""

    def test_unknown_keys_do_not_raise(self, manager, tmp_path):
        """Deserializer must not raise TypeError on unknown keys."""
        cfg = manager._dict_to_server_config(
            _build_dict_with_unknown_keys(str(tmp_path))
        )
        assert cfg is not None

    def test_known_keys_deserialize_correctly(self, manager, tmp_path):
        """The two new fields must deserialize to their provided values."""
        cfg = manager._dict_to_server_config(
            _build_dict_with_unknown_keys(str(tmp_path))
        )
        ci = cfg.claude_integration_config
        assert ci.dep_map_fact_check_enabled is True
        assert ci.fact_check_timeout_seconds == CUSTOM_TIMEOUT_SECONDS

    def test_unknown_keys_are_silently_dropped(self, manager, tmp_path):
        """After deserialization, unknown keys must not appear as attributes."""
        cfg = manager._dict_to_server_config(
            _build_dict_with_unknown_keys(str(tmp_path))
        )
        ci = cfg.claude_integration_config
        assert not hasattr(ci, "some_future_field_old_code_doesnt_know")
        assert not hasattr(ci, "another_future_field")
        assert not hasattr(ci, "yet_another_nested_thing")


class TestRollingUpgradeDefaults:
    """Default-value safety: old blobs (missing new fields) must use safe defaults."""

    def test_defaults_when_fields_absent(self, manager, tmp_path):
        """Old blob with no fact-check fields still deserializes with safe defaults."""
        cfg = manager._dict_to_server_config(
            {"server_dir": str(tmp_path), "claude_integration_config": {}}
        )
        ci = cfg.claude_integration_config
        assert ci.dep_map_fact_check_enabled is False
        assert ci.fact_check_timeout_seconds == DEFAULT_TIMEOUT_SECONDS
