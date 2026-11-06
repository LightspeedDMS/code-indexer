"""
Unit tests for Story #359: Refinement configuration fields.

Tests cover Component 9:
- Default values for 3 new fields on ClaudeIntegrationConfig
- the config API / screen lists the refinement settings
- the feature config door updates them

TDD RED PHASE: Tests written before production code exists.
"""

from tests.unit.server._depmap_feature import (
    depmap_claude_config,
    depmap_section_of,
    set_depmap_settings,
)
import pytest

from code_indexer.server.features import config as feature_config
from code_indexer.server.services.config_service import ConfigService


class TestDefaultRefinementConfig:
    """Default values for the 3 new refinement config fields."""

    def test_default_refinement_enabled_is_false(self):
        """refinement_enabled defaults to False (opt-in behavior)."""
        config = depmap_claude_config()
        assert config.refinement_enabled is False

    def test_default_refinement_interval_hours_is_24(self):
        """refinement_interval_hours defaults to 24 hours."""
        config = depmap_claude_config()
        assert config.refinement_interval_hours == 24

    def test_default_refinement_domains_per_run_is_3(self):
        """refinement_domains_per_run defaults to 3."""
        config = depmap_claude_config()
        assert config.refinement_domains_per_run == 3

    def test_refinement_fields_independent_of_dependency_map(self):
        """Refinement fields are separate from existing dependency_map fields."""
        config = depmap_claude_config(
            dependency_map_enabled=True,
            refinement_enabled=False,
        )
        assert config.dependency_map_enabled is True
        assert config.refinement_enabled is False

    def test_refinement_fields_settable_at_construction(self):
        """All 3 refinement fields can be set at construction time."""
        config = depmap_claude_config(
            refinement_enabled=True,
            refinement_interval_hours=48,
            refinement_domains_per_run=5,
        )
        assert config.refinement_enabled is True
        assert config.refinement_interval_hours == 48
        assert config.refinement_domains_per_run == 5


class TestRefinementSettingsListed:
    """The config API / screen lists the refinement keys at their defaults
    (the cidx-depmap feature's section since modular phase 4g)."""

    def test_refinement_defaults_listed(self, tmp_path):
        from cidx_depmap.config import DEPMAP_CONFIG_SECTION

        service = ConfigService(server_dir_path=str(tmp_path))
        (section,) = feature_config.describe_sections(
            [("depmap", DEPMAP_CONFIG_SECTION)], service.get_config()
        )
        values = {key["name"]: key["value"] for key in section["keys"]}
        assert values["refinement_enabled"] is False
        assert values["refinement_interval_hours"] == 24
        assert values["refinement_domains_per_run"] == 3


class TestRefinementSettingsUpdates:
    """The feature config door handles the refinement keys."""

    def test_update_refinement_enabled_true_then_false(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        set_depmap_settings(service, refinement_enabled=True)
        assert depmap_section_of(service).refinement_enabled is True
        set_depmap_settings(service, refinement_enabled=False)
        assert depmap_section_of(service).refinement_enabled is False

    def test_update_refinement_interval_hours(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        set_depmap_settings(service, refinement_interval_hours=48)
        assert depmap_section_of(service).refinement_interval_hours == 48

    def test_update_refinement_domains_per_run(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        set_depmap_settings(service, refinement_domains_per_run=5)
        assert depmap_section_of(service).refinement_domains_per_run == 5

    @pytest.mark.parametrize(
        "key,value",
        [
            ("refinement_interval_hours", 0),
            ("refinement_domains_per_run", 0),
            ("refinement_domains_per_run", 51),
        ],
    )
    def test_update_out_of_bounds_refused(self, tmp_path, key, value):
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(feature_config.FeatureConfigError, match="between"):
            set_depmap_settings(service, **{key: value})

    def test_unknown_refinement_setting_rejected(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(feature_config.FeatureConfigError, match="no setting"):
            set_depmap_settings(service, refinement_unknown_field="value")
