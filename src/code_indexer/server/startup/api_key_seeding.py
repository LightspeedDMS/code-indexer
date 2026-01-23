"""
API Key Auto-Seeding on Server Startup.

Provides functionality to auto-seed API keys from environment variables
and config files when the server starts and config keys are blank.

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def seed_api_keys_on_startup(
    config_service: Any,
    claude_config_path: Optional[str] = None,
    systemd_env_path: Optional[str] = None,
) -> Dict[str, bool]:
    """
    Auto-seed API keys on server startup if server config is blank.

    This function should be called during server startup (lifespan function)
    to populate API keys from environment variables or config files when
    the server's persisted configuration doesn't have them.

    Args:
        config_service: The ConfigService instance for accessing server config
        claude_config_path: Optional path to ~/.claude.json (for testing)
        systemd_env_path: Optional path to systemd env file (for testing)

    Returns:
        Dict with keys:
        - anthropic_seeded: True if Anthropic key was seeded
        - voyageai_seeded: True if VoyageAI key was seeded
    """
    from code_indexer.server.services.api_key_management import (
        ApiKeyAutoSeeder,
        ApiKeySyncService,
    )

    result = {
        "anthropic_seeded": False,
        "voyageai_seeded": False,
    }

    try:
        # Get current server config
        config = config_service.get_config()

        # Initialize seeder and sync service
        auto_seeder = ApiKeyAutoSeeder()

        # Build sync service kwargs
        sync_kwargs = {}
        if claude_config_path:
            sync_kwargs["claude_config_path"] = claude_config_path
        if systemd_env_path:
            sync_kwargs["systemd_env_path"] = systemd_env_path

        sync_service = ApiKeySyncService(**sync_kwargs)

        # Auto-seed Anthropic key if blank
        if not config.claude_integration_config.anthropic_api_key:
            seeded_anthropic_key = auto_seeder.get_anthropic_key()
            if seeded_anthropic_key:
                sync_result = sync_service.sync_anthropic_key(seeded_anthropic_key)
                if sync_result.success:
                    config.claude_integration_config.anthropic_api_key = (
                        seeded_anthropic_key
                    )
                    result["anthropic_seeded"] = True
                    logger.info(
                        "Auto-seeded Anthropic API key from environment/config"
                    )

        # Auto-seed VoyageAI key if blank
        if not config.claude_integration_config.voyageai_api_key:
            seeded_voyageai_key = auto_seeder.get_voyageai_key()
            if seeded_voyageai_key:
                sync_result = sync_service.sync_voyageai_key(seeded_voyageai_key)
                if sync_result.success:
                    config.claude_integration_config.voyageai_api_key = (
                        seeded_voyageai_key
                    )
                    result["voyageai_seeded"] = True
                    logger.info("Auto-seeded VoyageAI API key from environment")

        # Save config if any keys were seeded
        if result["anthropic_seeded"] or result["voyageai_seeded"]:
            config_service.config_manager.save_config(config)
            logger.info("Saved auto-seeded API keys to server config")

    except Exception as e:
        logger.warning(f"Failed to auto-seed API keys on startup: {e}")

    return result
