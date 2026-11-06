"""Story #927 CRITICAL #1: persistence of dep_map_auto_repair_enabled.

The key is the cidx-depmap feature's (modular phase 4g): it is listed and
changed through the feature config door, as every feature setting is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.features import config as feature_config
from tests.unit.server._depmap_feature import depmap_section_of, set_depmap_settings


def _make_config_service(tmp_dir: Path):
    """Create a ConfigService backed by a temp directory."""
    from code_indexer.server.services.config_service import ConfigService

    return ConfigService(server_dir_path=str(tmp_dir))


def _listed_values(svc: Any) -> Dict[str, Any]:
    """The depmap section's values as the config API and screen list them."""
    from cidx_depmap.config import DEPMAP_CONFIG_SECTION

    (section,) = feature_config.describe_sections(
        [("depmap", DEPMAP_CONFIG_SECTION)], svc.get_config()
    )
    return {key["name"]: key["value"] for key in section["keys"]}


class TestRoundTripPersistence:
    """Write then read must reflect the written value."""

    def test_round_trip_true(self, tmp_path):
        svc = _make_config_service(tmp_path)
        set_depmap_settings(svc, dep_map_auto_repair_enabled=True)
        assert depmap_section_of(svc).dep_map_auto_repair_enabled is True

    def test_round_trip_false_after_true(self, tmp_path):
        svc = _make_config_service(tmp_path)
        set_depmap_settings(svc, dep_map_auto_repair_enabled=True)
        set_depmap_settings(svc, dep_map_auto_repair_enabled=False)
        assert depmap_section_of(svc).dep_map_auto_repair_enabled is False


class TestListedVisibility:
    """The config API / screen lists dep_map_auto_repair_enabled and
    dep_map_fact_check_enabled."""

    def test_auto_repair_and_fact_check_fields_present(self, tmp_path):
        values = _listed_values(_make_config_service(tmp_path))
        assert "dep_map_auto_repair_enabled" in values
        assert "dep_map_fact_check_enabled" in values

    def test_auto_repair_default_is_false(self, tmp_path):
        assert (
            _listed_values(_make_config_service(tmp_path))[
                "dep_map_auto_repair_enabled"
            ]
            is False
        )


class TestUpdateAcceptance:
    """A bool is accepted; a string is not (the Web UI form converts its
    checkbox to a bool before the door)."""

    def test_bool_is_accepted(self, tmp_path):
        svc = _make_config_service(tmp_path)
        set_depmap_settings(svc, dep_map_auto_repair_enabled=True)

    def test_string_is_refused(self, tmp_path):
        svc = _make_config_service(tmp_path)
        with pytest.raises(feature_config.FeatureConfigError, match="bool"):
            set_depmap_settings(svc, dep_map_auto_repair_enabled="true")
