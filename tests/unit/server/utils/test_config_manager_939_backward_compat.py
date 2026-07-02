"""
Unit tests for Bug #939 backward compatibility: legacy file_content_limits_config key.

AC10: A config.json containing the legacy 'file_content_limits_config' key must load
cleanly (no exception raised) and the loaded ServerConfig must treat the field as absent
(None), because FileContentLimitsConfig has been deleted and the key is now obsolete.
"""

import json

from code_indexer.server.utils.config_manager import ServerConfigManager


def test_loading_config_with_legacy_key_does_not_raise(tmp_path):
    """Loading a config dict with legacy file_content_limits_config key must not raise."""
    config_file = tmp_path / "config.json"
    legacy_dict = {
        "server_dir": str(tmp_path),
        "file_content_limits_config": {
            "max_tokens_per_request": 5000,
            "chars_per_token": 4,
        },
    }
    config_file.write_text(json.dumps(legacy_dict), encoding="utf-8")

    manager = ServerConfigManager(str(tmp_path))
    config = manager.load_config()
    assert config is not None


def test_loading_strips_legacy_key_silently(tmp_path):
    """After loading, file_content_limits_config is None — the key is silently ignored."""
    config_file = tmp_path / "config.json"
    legacy_dict = {
        "server_dir": str(tmp_path),
        "file_content_limits_config": {
            "max_tokens_per_request": 9999,
            "chars_per_token": 7,
        },
    }
    config_file.write_text(json.dumps(legacy_dict), encoding="utf-8")

    manager = ServerConfigManager(str(tmp_path))
    config = manager.load_config()

    assert config is not None
    # After deletion of FileContentLimitsConfig the field must be absent entirely.
    assert not hasattr(config, "file_content_limits_config"), (
        "Legacy file_content_limits_config key must be silently stripped on load; "
        "the field must not exist on ServerConfig at all (Bug #939 completion)"
    )
