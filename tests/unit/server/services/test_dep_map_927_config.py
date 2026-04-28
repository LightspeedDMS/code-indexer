"""Story #927: dep_map_auto_repair_enabled config field default + presence."""

from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


class TestDepMapAutoRepairConfigField:
    def test_default_is_false(self):
        cfg = ClaudeIntegrationConfig()
        assert cfg.dep_map_auto_repair_enabled is False

    def test_can_be_set_true(self):
        cfg = ClaudeIntegrationConfig(dep_map_auto_repair_enabled=True)
        assert cfg.dep_map_auto_repair_enabled is True

    def test_field_is_bool_type(self):
        cfg = ClaudeIntegrationConfig()
        assert isinstance(cfg.dep_map_auto_repair_enabled, bool)
