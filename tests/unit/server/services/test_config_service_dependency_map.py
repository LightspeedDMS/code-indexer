"""
Unit tests for the dependency map's numeric settings through the feature
config door.

The numeric dependency map fields have lower bounds only (no upper bound:
users can set their own values freely). They are the cidx-depmap feature's
section (modular phase 4g), changed through the generic feature config door,
which refuses a value below the bound (the Claude CLI form clamped it).
"""

import pytest

from code_indexer.server.features.config import FeatureConfigError
from code_indexer.server.services.config_service import ConfigService
from tests.unit.server._depmap_feature import depmap_section_of, set_depmap_settings


@pytest.mark.parametrize(
    "key,below,large",
    [
        ("dependency_map_interval_hours", 0, 10000),
        ("dependency_map_pass_timeout_seconds", 30, 10000),
        ("dependency_map_pass1_max_turns", -1, 500),
        ("dependency_map_pass2_max_turns", -1, 500),
        ("dependency_map_delta_max_turns", -1, 500),
    ],
)
def test_numeric_setting_has_a_lower_bound_only(tmp_path, key, below, large):
    service = ConfigService(server_dir_path=str(tmp_path))

    with pytest.raises(FeatureConfigError, match="must be between"):
        set_depmap_settings(service, **{key: below})
    set_depmap_settings(service, **{key: large})

    assert getattr(depmap_section_of(service), key) == large


@pytest.mark.parametrize(
    "value,accepted", [(10, False), (60, True), (3600, True), (3601, False)]
)
def test_fact_check_timeout_is_bounded_60_to_3600(tmp_path, value, accepted):
    """Story #724: the verification timeout keeps the form's 60..3600 range."""
    service = ConfigService(server_dir_path=str(tmp_path))

    if accepted:
        set_depmap_settings(service, fact_check_timeout_seconds=value)
        assert depmap_section_of(service).fact_check_timeout_seconds == value
    else:
        with pytest.raises(FeatureConfigError, match="between 60 and 3600"):
            set_depmap_settings(service, fact_check_timeout_seconds=value)


def test_dependency_map_interval_hours_accepts_valid_value(tmp_path):
    service = ConfigService(server_dir_path=str(tmp_path))

    set_depmap_settings(service, dependency_map_interval_hours=168)

    assert depmap_section_of(service).dependency_map_interval_hours == 168


def test_dependency_map_pass3_setting_rejected(tmp_path):
    """dependency_map_pass3_max_turns is not a setting."""
    service = ConfigService(server_dir_path=str(tmp_path))

    with pytest.raises(FeatureConfigError, match="no setting"):
        set_depmap_settings(service, dependency_map_pass3_max_turns=30)


def test_all_dependency_map_max_turns_accept_valid_values(tmp_path):
    service = ConfigService(server_dir_path=str(tmp_path))

    set_depmap_settings(
        service,
        dependency_map_pass1_max_turns=50,
        dependency_map_pass2_max_turns=60,
        dependency_map_delta_max_turns=30,
    )

    claude_config = depmap_section_of(service)
    assert claude_config.dependency_map_pass1_max_turns == 50
    assert claude_config.dependency_map_pass2_max_turns == 60
    assert claude_config.dependency_map_delta_max_turns == 30


def test_the_claude_cli_category_no_longer_takes_dependency_map_keys(tmp_path):
    """Only the feature config door changes a feature's settings."""
    service = ConfigService(server_dir_path=str(tmp_path))

    with pytest.raises(ValueError, match="Unknown claude_cli setting"):
        service.update_setting("claude_cli", "dependency_map_interval_hours", 24)
