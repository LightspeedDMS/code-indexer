"""Hotfix: config_service wiring for web_security.self_registration_enabled.

The flag gating POST /auth/register must be readable via get_all_settings()
(so the Web UI Config screen can display it) and settable via the existing
"web_security" category from form strings ("true"/"false").
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


def _flag(service: ConfigService) -> bool:
    web_sec = service.get_config().web_security_config
    assert web_sec is not None
    return web_sec.self_registration_enabled


@pytest.fixture
def svc(tmp_path) -> ConfigService:
    service = ConfigService(server_dir_path=str(tmp_path))
    service.load_config()
    return service


def test_getter_exposes_self_registration_enabled_default_false(svc):
    settings = svc.get_all_settings()
    assert settings["web_security"]["self_registration_enabled"] is False


def test_getter_still_exposes_web_session_timeout(svc):
    settings = svc.get_all_settings()
    assert "web_session_timeout_seconds" in settings["web_security"]


def test_update_setting_true_string_enables(svc):
    svc.update_setting("web_security", "self_registration_enabled", "true")
    assert _flag(svc) is True
    assert svc.get_all_settings()["web_security"]["self_registration_enabled"] is True


def test_update_setting_false_string_disables(svc):
    svc.update_setting("web_security", "self_registration_enabled", "true")
    svc.update_setting("web_security", "self_registration_enabled", "false")
    assert _flag(svc) is False


def test_update_setting_persists_across_reload(tmp_path, svc):
    svc.update_setting("web_security", "self_registration_enabled", "true")
    reloaded = ConfigService(server_dir_path=str(tmp_path))
    reloaded.load_config()
    assert _flag(reloaded) is True


def test_atomic_update_path_used_by_web_ui_sets_flag(svc):
    """update_config_section() (POST /admin/config/{section}) applies the
    form via update_settings_atomic -- the flag must round-trip there too."""
    svc.update_settings_atomic([("web_security", "self_registration_enabled", "true")])
    assert _flag(svc) is True
    svc.update_settings_atomic([("web_security", "self_registration_enabled", "false")])
    assert _flag(svc) is False


def test_unknown_web_security_key_still_rejected(svc):
    with pytest.raises(ValueError, match="Unknown web security setting"):
        svc.update_setting("web_security", "no_such_key", "true")
