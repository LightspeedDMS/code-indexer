"""
Tests for API key seeding env sync behavior.

Validates that when server config already has API keys, they are synced to
os.environ on startup so embedding/AI services read the correct values.

Root cause fix: voyage_ai.py reads os.getenv("VOYAGE_API_KEY") which could
hold a stale shell env var when config.json has the valid key.
"""

import os
import pytest
from unittest.mock import MagicMock, patch


def _make_config(anthropic_key: str = "", voyageai_key: str = "") -> MagicMock:
    """Build a minimal mock config object."""
    claude_integration = MagicMock()
    claude_integration.anthropic_api_key = anthropic_key
    claude_integration.voyageai_api_key = voyageai_key

    config = MagicMock()
    config.claude_integration_config = claude_integration
    return config


def _make_config_service(config: MagicMock) -> MagicMock:
    """Build a minimal mock config_service."""
    config_service = MagicMock()
    config_service.get_config.return_value = config
    config_service.config_manager = MagicMock()
    return config_service


class TestApiKeySeedingEnvSync:
    """Tests for API key syncing from config to os.environ on startup."""

    def test_voyageai_key_in_config_is_synced_to_environ(self):
        """When config has a VoyageAI key, it must be written to os.environ."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        config = _make_config(voyageai_key="pa-valid-voyageai-key")
        config_service = _make_config_service(config)

        # Remove any existing env var to ensure our logic sets it
        env_backup = os.environ.pop("VOYAGE_API_KEY", None)
        try:
            result = seed_api_keys_on_startup(config_service)

            assert os.environ.get("VOYAGE_API_KEY") == "pa-valid-voyageai-key"
            assert result["voyageai_seeded"] is False  # not seeded — was already set
        finally:
            if env_backup is not None:
                os.environ["VOYAGE_API_KEY"] = env_backup
            else:
                os.environ.pop("VOYAGE_API_KEY", None)

    def test_anthropic_key_in_config_is_synced_to_environ(self):
        """When config has an Anthropic key, it must be written to os.environ."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        config = _make_config(anthropic_key="sk-ant-valid-anthropic-key")
        config_service = _make_config_service(config)

        env_backup = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            result = seed_api_keys_on_startup(config_service)

            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-valid-anthropic-key"
            assert result["anthropic_seeded"] is False  # not seeded — was already set
        finally:
            if env_backup is not None:
                os.environ["ANTHROPIC_API_KEY"] = env_backup
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_config_key_overwrites_stale_environ(self):
        """Config is source of truth — it overwrites a stale/invalid env var."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        config = _make_config(
            anthropic_key="sk-ant-correct",
            voyageai_key="pa-correct",
        )
        config_service = _make_config_service(config)

        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stale"
        os.environ["VOYAGE_API_KEY"] = "pa-stale"
        try:
            seed_api_keys_on_startup(config_service)

            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-correct"
            assert os.environ["VOYAGE_API_KEY"] == "pa-correct"
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("VOYAGE_API_KEY", None)

    def test_blank_voyageai_config_triggers_auto_seed(self):
        """When config VoyageAI key is blank, auto-seeding path runs as before."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        config = _make_config(voyageai_key="")
        config_service = _make_config_service(config)

        seeder_mock = MagicMock()
        seeder_mock.get_voyageai_key.return_value = "pa-seeded-from-env"

        sync_mock = MagicMock()
        sync_result = MagicMock()
        sync_result.success = True
        sync_mock.sync_voyageai_key.return_value = sync_result
        sync_mock.sync_anthropic_key.return_value = sync_result

        with patch(
            "code_indexer.server.startup.api_key_seeding.ApiKeyAutoSeeder",
            return_value=seeder_mock,
        ), patch(
            "code_indexer.server.startup.api_key_seeding.ApiKeySyncService",
            return_value=sync_mock,
        ):
            result = seed_api_keys_on_startup(config_service)

        assert result["voyageai_seeded"] is True

    def test_blank_anthropic_config_triggers_auto_seed(self):
        """When config Anthropic key is blank, auto-seeding path runs as before."""
        from code_indexer.server.startup.api_key_seeding import seed_api_keys_on_startup

        config = _make_config(anthropic_key="")
        config_service = _make_config_service(config)

        seeder_mock = MagicMock()
        seeder_mock.get_anthropic_key.return_value = "sk-ant-seeded-from-env"
        seeder_mock.get_voyageai_key.return_value = None

        sync_mock = MagicMock()
        sync_result = MagicMock()
        sync_result.success = True
        sync_mock.sync_anthropic_key.return_value = sync_result

        with patch(
            "code_indexer.server.startup.api_key_seeding.ApiKeyAutoSeeder",
            return_value=seeder_mock,
        ), patch(
            "code_indexer.server.startup.api_key_seeding.ApiKeySyncService",
            return_value=sync_mock,
        ):
            result = seed_api_keys_on_startup(config_service)

        assert result["anthropic_seeded"] is True
