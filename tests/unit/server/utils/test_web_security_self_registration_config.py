"""Hotfix: WebSecurityConfig.self_registration_enabled -- fail-closed default.

POST /auth/register used to let any unauthenticated caller create a live
normal_user account with no way to turn it off. The new flag gates that
endpoint and MUST default to False (closed), including for stored configs
written before the field existed.
"""

from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    WebSecurityConfig,
)


def test_web_security_config_self_registration_defaults_to_false():
    assert WebSecurityConfig().self_registration_enabled is False


def test_server_config_default_web_security_has_self_registration_disabled():
    config = ServerConfig(server_dir="/does-not-matter")
    assert config.web_security_config is not None
    assert config.web_security_config.self_registration_enabled is False


def test_stored_web_security_dict_without_key_loads_as_false(tmp_path):
    """A config persisted before this hotfix (no self_registration_enabled
    key) must load with registration DISABLED, never enabled."""
    manager = ServerConfigManager(str(tmp_path))
    persisted = {
        "server_dir": str(tmp_path),
        "web_security_config": {
            "web_session_timeout_seconds": 28800,
            "admin_session_timeout_seconds": 3600,
            "restrict_non_sso_to_web_ui": False,
        },
    }
    config = manager._dict_to_server_config(persisted)
    assert isinstance(config.web_security_config, WebSecurityConfig)
    assert config.web_security_config.self_registration_enabled is False


def test_stored_web_security_dict_with_true_round_trips(tmp_path):
    manager = ServerConfigManager(str(tmp_path))
    persisted = {
        "server_dir": str(tmp_path),
        "web_security_config": {"self_registration_enabled": True},
    }
    config = manager._dict_to_server_config(persisted)
    assert config.web_security_config is not None
    assert config.web_security_config.self_registration_enabled is True
