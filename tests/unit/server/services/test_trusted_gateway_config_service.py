"""EVO-66121: ConfigService get_all_settings() + update_setting() for trusted_gateway.

The trusted mcp-gateway bearer section must be readable (Web UI / config API) and
settable + persisted, so an admin can enable per-user gateway auth without hand-editing
the config store.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfigManager


def _service(tmp_path):
    mgr = ServerConfigManager(str(tmp_path))
    mgr.save_config(mgr.create_default_config())
    return ConfigService(str(tmp_path))


class TestTrustedGatewayConfigService:
    def test_get_all_settings_includes_trusted_gateway(self, tmp_path):
        settings = _service(tmp_path).get_all_settings()
        assert "trusted_gateway" in settings
        tg = settings["trusted_gateway"]
        assert tg["enabled"] is False
        assert tg["username_claim"] == "preferred_username"
        assert tg["leeway_seconds"] == 60
        # exposes the fields the validator + provisioning need
        for k in ("issuer", "jwks_uri", "audience", "groups_claim", "default_role"):
            assert k in tg

    def test_update_enabled_persists(self, tmp_path):
        svc = _service(tmp_path)
        svc.update_setting("trusted_gateway", "enabled", "true")
        assert svc.get_config().trusted_gateway_config.enabled is True

    def test_update_string_fields_persist(self, tmp_path):
        svc = _service(tmp_path)
        svc.update_setting(
            "trusted_gateway", "issuer", "https://mcp.test.lightspeedtools.cloud"
        )
        svc.update_setting("trusted_gateway", "audience", "cidx-tools-test")
        svc.update_setting("trusted_gateway", "leeway_seconds", 30)
        tg = svc.get_config().trusted_gateway_config
        assert tg.issuer == "https://mcp.test.lightspeedtools.cloud"
        assert tg.audience == "cidx-tools-test"
        assert tg.leeway_seconds == 30

    def test_update_group_mappings_json_parsed(self, tmp_path):
        svc = _service(tmp_path)
        svc.update_setting(
            "trusted_gateway",
            "group_mappings",
            '[{"external_group_id": "g1", "cidx_group": "admins"}]',
        )
        assert svc.get_config().trusted_gateway_config.group_mappings == [
            {"external_group_id": "g1", "cidx_group": "admins"}
        ]

    def test_update_unknown_key_raises(self, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("trusted_gateway", "nope", "x")

    def test_malformed_group_mappings_json_raises(self, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(ValueError):
            svc.update_setting("trusted_gateway", "group_mappings", "{not valid json")

    def test_non_numeric_leeway_raises(self, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises((ValueError, TypeError)):
            svc.update_setting("trusted_gateway", "leeway_seconds", "not-a-number")
