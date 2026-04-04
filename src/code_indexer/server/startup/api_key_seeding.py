"""
API Key Sync on Server Startup.

Syncs API keys from server config to the process environment on startup.
Config is the source of truth — keys are never read from environment
variables and written back into config.

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def seed_api_keys_on_startup(
    config_service: Any,
    claude_config_path: Optional[str] = None,
) -> Dict[str, bool]:
    """
    Sync API keys from server config to process environment on startup.

    Config is the source of truth. If config has a key, it is synced to
    os.environ so that subprocesses (Claude CLI, VoyageAI SDK) can use it.
    If config is blank, no action is taken — keys are never auto-seeded
    from environment variables back into config.

    Args:
        config_service: The ConfigService instance for accessing server config
        claude_config_path: Optional path to ~/.claude.json (for testing)

    Returns:
        Dict with keys:
        - anthropic_seeded: Always False (nothing is seeded into config)
        - voyageai_seeded: Always False (nothing is seeded into config)
        - cohere_seeded: True if Cohere key was synced, False otherwise
    """
    from code_indexer.server.services.api_key_management import ApiKeySyncService

    result = {
        "anthropic_seeded": False,
        "voyageai_seeded": False,
        "cohere_seeded": False,
    }

    try:
        config = config_service.get_config()

        # Build sync service kwargs
        sync_kwargs: Dict[str, str] = {}
        if claude_config_path:
            sync_kwargs["claude_config_path"] = claude_config_path
        sync_service = ApiKeySyncService(**sync_kwargs)

        # Anthropic: config → env (unidirectional)
        # Config has key → sync to env; config blank → clear env
        if config.claude_integration_config.anthropic_api_key:
            sync_service.sync_anthropic_key(
                config.claude_integration_config.anthropic_api_key
            )
            logger.info(
                "Synced Anthropic API key from server config to process environment"
            )
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            logger.info(
                "Cleared Anthropic API key from process environment (config is blank)"
            )

        # VoyageAI: config → env (unidirectional)
        # Config has key → set env; config blank → clear env
        if config.claude_integration_config.voyageai_api_key:
            os.environ["VOYAGE_API_KEY"] = (
                config.claude_integration_config.voyageai_api_key
            )
            result["voyageai_seeded"] = True
            logger.info(
                "Synced VoyageAI API key from server config to process environment"
            )
        else:
            os.environ.pop("VOYAGE_API_KEY", None)
            logger.info(
                "Cleared VoyageAI API key from process environment (config is blank)"
            )

        # Cohere: config → env (unidirectional)
        # Config has key → set env; config blank → clear env
        if config.claude_integration_config.cohere_api_key:
            os.environ["CO_API_KEY"] = config.claude_integration_config.cohere_api_key
            result["cohere_seeded"] = True
            logger.info(
                "Synced Cohere API key from server config to process environment"
            )
        else:
            os.environ.pop("CO_API_KEY", None)
            logger.info(
                "Cleared Cohere API key from process environment (config is blank)"
            )

    except Exception as e:
        from code_indexer.server.logging_utils import format_error_log

        logger.warning(
            format_error_log(
                "MCP-GENERAL-194", f"Failed to sync API keys on startup: {e}"
            )
        )

    return result
