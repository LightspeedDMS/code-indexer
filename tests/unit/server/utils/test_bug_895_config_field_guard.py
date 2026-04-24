"""
Anti-regression guard for Bug #895.

Asserts that fields declared on ClaudeIntegrationConfig do NOT exist as
direct attributes on ServerConfig. If any field name collides, getattr on
a ServerConfig instance would silently return the field value instead of
raising AttributeError, which is the root cause of Bug #895.
"""

import dataclasses
import pytest

from code_indexer.server.utils.config_manager import (
    ClaudeIntegrationConfig,
    ServerConfig,
)


_CI_FIELDS = [f.name for f in dataclasses.fields(ClaudeIntegrationConfig)]


class TestConfigFieldsNotReachableOnServerConfig:
    """Guard: ClaudeIntegrationConfig fields must NOT exist on top-level ServerConfig."""

    @pytest.mark.parametrize("field_name", _CI_FIELDS)
    def test_field_not_on_server_config(self, field_name, tmp_path):
        """hasattr(ServerConfig(), <ClaudeIntegrationConfig field>) must be False."""
        server_config = ServerConfig(server_dir=str(tmp_path))
        assert not hasattr(server_config, field_name), (
            f"Bug #895 regression: ServerConfig has attribute '{field_name}' which "
            f"is also declared on ClaudeIntegrationConfig. "
            f"getattr(server_config, '{field_name}', default) would silently return "
            f"the server_config value instead of the nested claude_integration_config value. "
            f"Remove the top-level alias or the field from ClaudeIntegrationConfig."
        )
