"""EVO-66121: TrustedGatewayConfig dataclass + ServerConfig(Manager) wiring.

Covers the new config-parsing logic flagged in MR !49 review: __post_init__ defaults +
group_mappings dict->list conversion, the ServerConfig default, and the
ServerConfigManager nested-dict -> TrustedGatewayConfig conversion.
"""

from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    TrustedGatewayConfig,
)


class TestTrustedGatewayConfigDataclass:
    def test_defaults_are_disabled_and_sane(self):
        c = TrustedGatewayConfig()
        assert c.enabled is False
        assert c.username_claim == "preferred_username"
        assert c.email_claim == "email"
        assert c.groups_claim == "groups"
        assert c.default_role == "normal_user"
        assert c.require_email_verification is True
        assert c.enable_jit_provisioning is True
        assert c.leeway_seconds == 60
        assert c.group_mappings == []  # None -> [] via __post_init__

    def test_group_mappings_dict_converted_to_list(self):
        c = TrustedGatewayConfig(group_mappings={"ext-group-1": "cidx-admins"})
        assert c.group_mappings == [
            {"external_group_id": "ext-group-1", "cidx_group": "cidx-admins"}
        ]

    def test_group_mappings_list_preserved(self):
        mappings = [{"external_group_id": "g", "cidx_group": "c"}]
        c = TrustedGatewayConfig(group_mappings=mappings)
        assert c.group_mappings == mappings


class TestServerConfigWiring:
    def test_server_config_defaults_trusted_gateway_config(self, tmp_path):
        cfg = ServerConfig(server_dir=str(tmp_path))
        assert isinstance(cfg.trusted_gateway_config, TrustedGatewayConfig)
        assert cfg.trusted_gateway_config.enabled is False

    def test_dict_to_server_config_converts_nested_dict(self, tmp_path):
        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "trusted_gateway_config": {
                "enabled": True,
                "issuer": "https://mcp.test.lightspeedtools.cloud",
                "jwks_uri": "https://mcp.test.lightspeedtools.cloud/oauth/jwks.json",
                "audience": "cidx-tools-test",
                "groups_claim": "roles",
                "leeway_seconds": 30,
            },
        }
        cfg = manager._dict_to_server_config(config_dict)
        tg = cfg.trusted_gateway_config
        assert isinstance(tg, TrustedGatewayConfig)
        assert tg.enabled is True
        assert tg.issuer == "https://mcp.test.lightspeedtools.cloud"
        assert tg.groups_claim == "roles"
        assert tg.leeway_seconds == 30
