"""
Unit tests for Workspace Cleanup Configuration (Story #647 - AC1).

Tests configurable retention period for SCIP self-healing workspace cleanup:
- Default retention period (7 days)
- Environment variable override (CIDX_SCIP_WORKSPACE_RETENTION_DAYS)
- Config file setting (scip_workspace_retention_days)
- Range validation (minimum 1 day, maximum 365 days)

Since modular phase 4d the setting is the cidx-scip feature's runtime key
(``feature_settings["scip"]``, its config section bounds it); a value stored
under the former ``scip_config`` (Story #15) is carried into the section when
the configuration loads.
"""

import os
from typing import Any, Dict, Iterator, cast

import pytest

from cidx_sdk.capabilities import CONFIG
from cidx_scip.config import SCIP_CONFIG_SECTION, WORKSPACE_RETENTION_DAYS
from cidx_scip.server import runtime
from cidx_scip.server.workspace_cleanup import WorkspaceCleanupService
from code_indexer.server.features.config import (
    FeatureConfigError,
    validate_runtime_update,
)
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
)


class _Section:
    def __init__(self, values: Dict[str, object]) -> None:
        self._values = values

    def section(self) -> Dict[str, object]:
        return dict(self._values)


class _Context:
    """A started feature's context holding only the ``config`` capability."""

    def __init__(self, values: Dict[str, object]) -> None:
        self._config = _Section(values)

    def has_capability(self, name: str) -> bool:
        return name == CONFIG

    def capability(self, name: str) -> object:
        return self._config


@pytest.fixture
def feature_section() -> Iterator[Any]:
    def install(values: Dict[str, object]) -> None:
        runtime.CONTEXT.install(cast(Any, _Context(values)))

    yield install
    runtime.CONTEXT.uninstall()


def _validate(value: object) -> Dict[str, Any]:
    return validate_runtime_update(
        "scip", SCIP_CONFIG_SECTION, {WORKSPACE_RETENTION_DAYS: value}
    )


class TestWorkspaceCleanupConfigAC1:
    """AC1: Configurable Retention Period tests."""

    def test_default_retention_period_is_7_days(self, monkeypatch):
        """The feature section's default (no started feature, no override)."""
        monkeypatch.delenv(runtime.RETENTION_ENV_VAR, raising=False)
        by_name = {key.name: key for key in SCIP_CONFIG_SECTION.keys}
        assert by_name[WORKSPACE_RETENTION_DAYS].default == 7
        assert runtime.workspace_retention_days() == 7
        service = WorkspaceCleanupService(job_manager=None)
        assert service.retention_days == 7

    def test_retention_period_via_config_file(self, tmp_path):
        """A config file still carrying the Story #15 nested value loads it
        into the feature's section (nothing is lost on upgrade)."""
        server_dir = tmp_path / "server"
        server_dir.mkdir(parents=True)
        (server_dir / "config.json").write_text(
            '{"scip_config": {"scip_workspace_retention_days": 14}}'
        )

        config = ServerConfigManager(str(server_dir)).load_config()

        assert config is not None
        assert config.feature_settings["scip"][WORKSPACE_RETENTION_DAYS] == 14
        assert not hasattr(config.scip_config, WORKSPACE_RETENTION_DAYS)

    def test_retention_period_from_the_feature_setting(
        self, feature_section, monkeypatch
    ):
        monkeypatch.delenv(runtime.RETENTION_ENV_VAR, raising=False)
        feature_section({WORKSPACE_RETENTION_DAYS: 14})
        assert runtime.workspace_retention_days() == 14
        assert WorkspaceCleanupService(job_manager=None).retention_days == 14

    def test_retention_period_via_environment_variable(
        self, feature_section, monkeypatch
    ):
        """CIDX_SCIP_WORKSPACE_RETENTION_DAYS overrides the setting."""
        feature_section({WORKSPACE_RETENTION_DAYS: 14})
        monkeypatch.setenv(runtime.RETENTION_ENV_VAR, "21")
        assert runtime.workspace_retention_days() == 21
        assert WorkspaceCleanupService(job_manager=None).retention_days == 21

    def test_invalid_environment_variable_falls_back_to_the_setting(
        self, feature_section, monkeypatch
    ):
        feature_section({WORKSPACE_RETENTION_DAYS: 14})
        monkeypatch.setenv(runtime.RETENTION_ENV_VAR, "not-a-number")
        assert runtime.workspace_retention_days() == 14

    def test_retention_period_minimum_validation(self):
        """AC1: minimum retention period is 1 day."""
        with pytest.raises(FeatureConfigError, match="between 1 and 365"):
            _validate(0)

    def test_retention_period_maximum_validation(self):
        """AC1: maximum retention period is 365 days."""
        with pytest.raises(FeatureConfigError, match="between 1 and 365"):
            _validate(366)

    def test_retention_period_valid_range(self):
        for value in (1, 30, 365):
            assert _validate(value) == {WORKSPACE_RETENTION_DAYS: value}

    def test_legacy_top_level_retention_moves_into_the_section(self, tmp_path):
        """A pre-Story #15 top-level value is carried into the section too."""
        server_dir = tmp_path / "server"
        server_dir.mkdir(parents=True)
        (server_dir / "config.json").write_text('{"scip_workspace_retention_days": 9}')

        config = ServerConfigManager(str(server_dir)).load_config()

        assert config is not None
        assert config.feature_settings["scip"][WORKSPACE_RETENTION_DAYS] == 9

    def test_retention_period_type_validation(self):
        """A non-integer value is refused."""
        with pytest.raises(FeatureConfigError):
            _validate("not an integer")

    def test_server_config_keeps_only_the_temporal_threshold(self, tmp_path):
        server_dir = str(tmp_path / "server")
        os.makedirs(server_dir, exist_ok=True)
        config = ServerConfig(server_dir=server_dir)
        assert config.scip_config is not None
        assert config.scip_config.temporal_stale_threshold_days == 7
        assert not hasattr(config.scip_config, WORKSPACE_RETENTION_DAYS)
