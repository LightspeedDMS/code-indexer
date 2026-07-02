"""
Unit tests for LangfuseConfig in ServerConfig (Story #136).

Tests configuration schema validation, defaults, and graceful handling
of disabled state.
"""

import pytest
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    LangfuseConfig,
)


class TestLangfuseConfig:
    """Test LangfuseConfig dataclass."""

    def test_default_values(self):
        """Test LangfuseConfig has correct defaults."""
        config = LangfuseConfig()
        assert config.enabled is False
        assert config.public_key == ""
        assert config.secret_key == ""
        assert config.host == "https://cloud.langfuse.com"
        assert config.auto_trace_enabled is False  # Story #136 follow-up

    def test_custom_values(self):
        """Test LangfuseConfig accepts custom values."""
        config = LangfuseConfig(
            enabled=True,
            public_key="pk-test-123",
            secret_key="sk-test-456",
            host="http://localhost:3000",
            auto_trace_enabled=True,
        )
        assert config.enabled is True
        assert config.public_key == "pk-test-123"
        assert config.secret_key == "sk-test-456"
        assert config.host == "http://localhost:3000"
        assert config.auto_trace_enabled is True  # Story #136 follow-up


class TestServerConfigLangfuseIntegration:
    """Test Langfuse configuration integration with ServerConfig."""

    def test_server_config_initializes_langfuse_config(self, tmp_path):
        """Test ServerConfig initializes langfuse_config with defaults."""
        config = ServerConfig(server_dir=str(tmp_path))
        assert config.langfuse_config is not None
        assert isinstance(config.langfuse_config, LangfuseConfig)
        assert config.langfuse_config.enabled is False

    def test_server_config_accepts_custom_langfuse_config(self, tmp_path):
        """Test ServerConfig accepts custom LangfuseConfig."""
        custom_config = LangfuseConfig(
            enabled=True,
            public_key="pk-custom",
            secret_key="sk-custom",
            host="http://custom:3000",
        )
        config = ServerConfig(server_dir=str(tmp_path), langfuse_config=custom_config)
        assert config.langfuse_config.enabled is True
        assert config.langfuse_config.public_key == "pk-custom"
        assert config.langfuse_config.secret_key == "sk-custom"
        assert config.langfuse_config.host == "http://custom:3000"


class TestLangfuseConfigValidation:
    """Test validation of Langfuse configuration values."""

    def test_validate_host_format(self, tmp_path):
        """Test validation of host URL format."""
        manager = ServerConfigManager(str(tmp_path))

        # Valid HTTP/HTTPS hosts should pass
        for host in [
            "http://localhost:3000",
            "https://cloud.langfuse.com",
            "http://192.168.1.100:3000",
        ]:
            config = ServerConfig(
                server_dir=str(tmp_path),
                langfuse_config=LangfuseConfig(
                    enabled=True,
                    public_key="pk-test",
                    secret_key="sk-test",
                    host=host,
                ),
            )
            manager.validate_config(config)  # Should not raise

        # Invalid host format should fail
        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                enabled=True,
                public_key="pk-test",
                secret_key="sk-test",
                host="invalid-url-without-protocol",
            ),
        )
        with pytest.raises(ValueError, match="must start with http:// or https://"):
            manager.validate_config(config)

    def test_validate_disabled_config(self, tmp_path):
        """Test validation passes when Langfuse is disabled."""
        manager = ServerConfigManager(str(tmp_path))

        # Disabled config with empty credentials should pass
        config = ServerConfig(
            server_dir=str(tmp_path),
            langfuse_config=LangfuseConfig(
                enabled=False,
                public_key="",
                secret_key="",
            ),
        )
        manager.validate_config(config)  # Should not raise
