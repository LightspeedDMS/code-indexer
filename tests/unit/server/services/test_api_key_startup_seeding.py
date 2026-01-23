"""
Unit tests for API key auto-seeding during server startup.

Tests cover:
- Auto-seeding invocation during server startup (lifespan function)
- Seeding only when server config keys are blank
- No seeding when keys already configured

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestApiKeyStartupSeeding:
    """Test auto-seeding of API keys during server startup."""

    def test_auto_seeder_seeds_anthropic_key_when_blank(self):
        """AC: Auto-seed Anthropic key from env when server config is blank."""
        from code_indexer.server.services.api_key_management import (
            ApiKeyAutoSeeder,
        )

        original_value = os.environ.pop("ANTHROPIC_API_KEY", None)

        try:
            test_key = "sk-ant-api03-startup12345678901234567890123"
            os.environ["ANTHROPIC_API_KEY"] = test_key

            seeder = ApiKeyAutoSeeder()
            result = seeder.get_anthropic_key()

            assert result == test_key
        finally:
            if original_value is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_value
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_auto_seeder_seeds_voyageai_key_when_blank(self):
        """AC: Auto-seed VoyageAI key from env when server config is blank."""
        from code_indexer.server.services.api_key_management import (
            ApiKeyAutoSeeder,
        )

        original_value = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            test_key = "pa-startupvoyage123456"
            os.environ["VOYAGE_API_KEY"] = test_key

            seeder = ApiKeyAutoSeeder()
            result = seeder.get_voyageai_key()

            assert result == test_key
        finally:
            if original_value is not None:
                os.environ["VOYAGE_API_KEY"] = original_value
            else:
                os.environ.pop("VOYAGE_API_KEY", None)

    def test_startup_seeding_function_exists(self):
        """Verify the startup seeding helper function can be imported."""
        # This test verifies the function exists and can be called
        from code_indexer.server.startup.api_key_seeding import (
            seed_api_keys_on_startup,
        )

        # The function should exist and be callable
        assert callable(seed_api_keys_on_startup)

    def test_startup_seeding_seeds_when_config_blank(self):
        """AC: Auto-seed invoked during startup when server config keys are blank."""
        from code_indexer.server.startup.api_key_seeding import (
            seed_api_keys_on_startup,
        )

        # Create mock config with blank keys
        mock_config = MagicMock()
        mock_config.claude_integration_config.anthropic_api_key = None
        mock_config.claude_integration_config.voyageai_api_key = None

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config

        # Set up environment with test keys
        original_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
        original_voyage = os.environ.pop("VOYAGE_API_KEY", None)

        try:
            test_anthropic = "sk-ant-api03-seedtest123456789012345678901"
            test_voyage = "pa-seedtestvoyage12345"
            os.environ["ANTHROPIC_API_KEY"] = test_anthropic
            os.environ["VOYAGE_API_KEY"] = test_voyage

            with tempfile.TemporaryDirectory() as tmpdir:
                result = seed_api_keys_on_startup(
                    mock_config_service,
                    claude_config_path=str(Path(tmpdir) / ".claude.json"),
                    systemd_env_path=str(Path(tmpdir) / "env"),
                )

            assert result["anthropic_seeded"] is True
            assert result["voyageai_seeded"] is True
        finally:
            if original_anthropic is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_anthropic
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            if original_voyage is not None:
                os.environ["VOYAGE_API_KEY"] = original_voyage
            else:
                os.environ.pop("VOYAGE_API_KEY", None)

    def test_startup_seeding_skips_when_config_has_keys(self):
        """AC: No seeding when server config already has keys."""
        from code_indexer.server.startup.api_key_seeding import (
            seed_api_keys_on_startup,
        )

        # Create mock config with existing keys
        mock_config = MagicMock()
        mock_config.claude_integration_config.anthropic_api_key = (
            "sk-ant-api03-existing123456789012345678901234"
        )
        mock_config.claude_integration_config.voyageai_api_key = (
            "pa-existingvoyage123"
        )

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config

        with tempfile.TemporaryDirectory() as tmpdir:
            result = seed_api_keys_on_startup(
                mock_config_service,
                claude_config_path=str(Path(tmpdir) / ".claude.json"),
                systemd_env_path=str(Path(tmpdir) / "env"),
            )

        assert result["anthropic_seeded"] is False
        assert result["voyageai_seeded"] is False
