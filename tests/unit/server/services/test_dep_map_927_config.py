"""Story #927: dep_map_auto_repair_enabled config field default + presence."""

import pytest as _pytest

from tests.unit.server._depmap_feature import (
    depmap_claude_config,
    depmap_registry_active,
)


class TestDepMapAutoRepairConfigField:
    def test_default_is_false(self):
        cfg = depmap_claude_config()
        assert cfg.dep_map_auto_repair_enabled is False

    def test_can_be_set_true(self):
        cfg = depmap_claude_config(dep_map_auto_repair_enabled=True)
        assert cfg.dep_map_auto_repair_enabled is True

    def test_field_is_bool_type(self):
        cfg = depmap_claude_config()
        assert isinstance(cfg.dep_map_auto_repair_enabled, bool)


@_pytest.fixture(autouse=True)
def _depmap_loaded():
    """The dependency-map settings are the cidx-depmap feature's section
    (modular phase 4g): the config service lists and updates them while the
    feature is loaded."""
    with depmap_registry_active():
        yield
